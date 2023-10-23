########################################################################################################################
# Description: This script is used to audit the parquet files in S3 against records OpenSearch. It will print out the  #
#              S3 keys of the files that are not found in OpenSearch.                                                  #
#                                                                                                                      #
# Usage:                                                                                                               #
#   python audit_tool.py [ -o <output_filename> ]                                                                      #
#                                                                                                                      #
# Environment variables:                                                                                               #
#   AWS_ACCESS_KEY_ID: AWS access key ID                                                                               #
#   AWS_SECRET_ACCESS_KEY: AWS secret access key                                                                       #
#   OPENSEARCH_ENDPOINT: OpenSearch endpoint                                                                           #
#   OPENSEARCH_PORT: OpenSearch port                                                                                   #
#   OPENSEARCH_ID_PREFIX: OpenSearch ID prefix e.g. s3://cdms-dev-in-situ-parquet                                      #
#   OPENSEARCH_INDEX: OpenSearch index e.g. [ parquet_stats_alias | entry_file_records_alias ]                         #
#   OPENSEARCH_PATH_PREFIX: OpenSearch path prefix (use '' if no prefix needed)                                        #
#   OPENSEARCH_BUCKET: OpenSearch bucket                                                                               #
########################################################################################################################


import boto3
from requests_aws4auth import AWS4Auth
from elasticsearch import Elasticsearch, RequestsHttpConnection, NotFoundError
import os
import sys
import argparse
import textwrap
import json
import logging

from parquet_flask.aws import AwsSQS, AwsSNS
from parquet_flask.cdms_lambda_func.lambda_logger_generator import LambdaLoggerGenerator


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(name)s::%(lineno)d] %(message)s'
)

logger = LambdaLoggerGenerator.get_logger(__name__)
logging.getLogger('elasticsearch').setLevel(logging.WARNING)


# Append a slash to the end of a string if it doesn't already have one
def append_slash(string: str):
    if string is None:
        return None
    elif string == '':
        return string
    elif string[-1] != '/':
        return string + '/'
    else:
        return string


def key_to_sqs_msg(key: str, bucket: str):
    s3_event = {
        'Records': [
            {
                'eventName': 'ObjectCreated:Put',
                's3': {
                    'bucket': {'name': bucket},
                    'object': {'key': key}
                }
            }
        ]
    }

    sqs_body = json.dumps(s3_event)

    return sqs_body


def audit(format='plain', output_file=None, sns_topic=None, resume_from_key=None, lambda_ctx=None):
    if format == 'mock-s3' and not output_file:
        raise ValueError('Output file MUST be defined with mock-s3 output format')

    # Check if AWS credentials are set
    AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID', None)
    AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY', None)
    AWS_SESSION_TOKEN = os.environ.get('AWS_SESSION_TOKEN', None)
    if AWS_ACCESS_KEY_ID is None or AWS_SECRET_ACCESS_KEY is None:
        logger.error('AWS credentials are not set. Please set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.')
        exit(1)

    # Check if OpenSearch parameters are set
    OPENSEARCH_ENDPOINT = os.environ.get('OPENSEARCH_ENDPOINT', None)
    OPENSEARCH_PORT = os.environ.get('OPENSEARCH_PORT', 443)
    OPENSEARCH_INDEX = os.environ.get('OPENSEARCH_INDEX', None)
    OPENSEARCH_PATH_PREFIX = append_slash(os.environ.get('OPENSEARCH_PATH_PREFIX', None))
    OPENSEARCH_BUCKET = os.environ.get('OPENSEARCH_BUCKET', None)
    OPENSEARCH_ID_PREFIX = append_slash(os.environ.get('OPENSEARCH_ID_PREFIX', f's3://{OPENSEARCH_BUCKET}/'))
    if OPENSEARCH_ENDPOINT is None or OPENSEARCH_PORT is None or OPENSEARCH_ID_PREFIX is None or OPENSEARCH_INDEX is None or OPENSEARCH_PATH_PREFIX is None or OPENSEARCH_BUCKET is None:
        logger.error('OpenSearch parameters are not set. Please set OPENSEARCH_ENDPOINT, OPENSEARCH_PORT, OPENSEARCH_ID_PREFIX, OPENSEARCH_INDEX, OPENSEARCH_PATH_PREFIX, OPENSEARCH_BUCKET.')
        exit(1)

    # AWS session
    aws_session_param = {}
    aws_session_param['aws_access_key_id'] = AWS_ACCESS_KEY_ID
    aws_session_param['aws_secret_access_key'] = AWS_SECRET_ACCESS_KEY

    if AWS_SESSION_TOKEN:
        aws_session_param['aws_session_token'] = AWS_SESSION_TOKEN

    aws_session = boto3.Session(**aws_session_param)

    # AWS auth
    aws_auth = AWS4Auth(
        aws_session.get_credentials().access_key,
        aws_session.get_credentials().secret_key,
        aws_session.region_name,
        'es',
        session_token=aws_session.get_credentials().token
    )

    # S3 paginator
    s3 = aws_session.client('s3')
    lambda_client = aws_session.client('lambda')

    paginator = s3.get_paginator('list_objects_v2')

    opensearch_client = Elasticsearch(
        hosts=[{'host': OPENSEARCH_ENDPOINT, 'port': OPENSEARCH_PORT}],
        http_auth=aws_auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection
    )

    # Go through all files in a bucket
    response_iterator = paginator.paginate(Bucket=OPENSEARCH_BUCKET, Prefix=OPENSEARCH_PATH_PREFIX)

    keys = []

    count = 0
    error_count = 0
    error_s3_keys = []
    missing_keys = []
    logger.info('processing... will print out S3 keys that cannot find a match...')
    for page in response_iterator:
        logger.info(f"Listed page of {len(page.get('Contents', [])):,} objects")

        for obj in page.get('Contents', []):
            keys.append(obj)

    if resume_from_key is not None and resume_from_key in keys:
        logger.info(f'Resuming audit from key {resume_from_key}')
        index = keys.index(resume_from_key)
    else:
        logger.info('Starting audit from the beginning')
        index = 0

    keys = keys[index:]

    need_to_resume = False

    for obj in keys:
        if lambda_ctx is not None and lambda_ctx.get_remaining_time_in_millis() < (60 * 1000):
            logger.warning('Lambda is about to time out, re-invoking to resume from this key')

            need_to_resume = True
            resume_from_key = obj['Key']

            break

        count += 1
        try:
            # Search key in opensearch
            opensearch_id = os.path.join(OPENSEARCH_ID_PREFIX + obj['Key'])
            opensearch_response = opensearch_client.get(index=OPENSEARCH_INDEX, id=opensearch_id)
            if opensearch_response is None or not type(opensearch_response) is dict or not opensearch_response['found']:
                error_count += 1
                error_s3_keys.append(obj['Key'])
                sys.stdout.write("\x1b[2k")
                logger.info(obj['Key'])
                missing_keys.append(obj['Key'])
        except NotFoundError as e:
            error_count += 1
            error_s3_keys.append(obj['Key'])
            sys.stdout.write("\x1b[2k")
            logger.info(obj['Key'])
            missing_keys.append(obj['Key'])
        except Exception as e:
            error_count += 1

        if count % 50 == 0:
            logger.info(f'Processed {count} files')

    logger.info(f'Processed {count} files')
    logger.info(f'Found {len(missing_keys):,} missing keys')

    if format == 'plain':
        if output_file:
            with open(output_file, 'w') as f:
                for key in missing_keys:
                    f.write(key + '\n')
        else:
            logger.info('Not writing to file as none was given')
    else:
        sqs_messages = [key_to_sqs_msg(k, OPENSEARCH_BUCKET) for k in missing_keys]

        sqs = AwsSQS()

        for m in sqs_messages:
            sqs.send_message(output_file, m)

        if sns_topic:
            sns = AwsSNS()

            sns.publish(
                sns_topic,
                f'Parquet stats audit found {len(missing_keys):,} missing keys. Trying to republish to SQS.',
                'Insitu audit'
            )

    if need_to_resume:
        response = lambda_client.invoke(
            FunctionName=lambda_ctx.function_name,
            InvocationType='Event',
            Payload=json.dumps({'ResumeFromKey': resume_from_key}).encode('utf-8')
        )

        print(response)


if __name__ == '__main__':
    # Parse arguments
    parser = argparse.ArgumentParser(
        description='Audit parquet files in S3 against records in OpenSearch',
        epilog=textwrap.dedent('''\
            Environment variables:  (describe what they are for & provide examples where appropriate
                AWS_ACCESS_KEY_ID : AWS access key ID for S3 bucket & OpenSearch index
                AWS_SECRET_ACCESS_KEY : AWS secret access key for S3 bucket & OpenSearch index
                AWS_REGION : AWS region for S3 bucket & OpenSearch index
                OPENSEARCH_ENDPOINT : Endpoint for OpenSearch domain
                OPENSEARCH_PORT : Port to connect to OpenSearch (Default: 443)
                OPENSEARCH_BUCKET : Name of the bucket storing ingested Parquet files.
                OPENSEARCH_PATH_PREFIX : Key prefix for objects in OPENSEARCH_BUCKET to audit.
                OPENSEARCH_ID_PREFIX : S3 URI prefix for the id field in OpenSearch documents. Defaults to 's3://<OPENSEARCH_BUCKET>/'
                OPENSEARCH_INDEX : OpenSearch index to audit
        '''),
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '-o', '--output-file',
        nargs='?',
        type=str,
        help='file to output the S3 keys of the files that are not found in OpenSearch',
        dest='output'
    )

    parser.add_argument(
        '-f', '--format',
        choices=['plain', 'mock-s3'],
        default='plain',
        dest='format',
        help='Output format. \'plain\' will output keys of missing parquet files to the output file in plain text. '
             '\'mock-s3\' will output missing keys to SQS (-o is required as the SQS queue URL), formatted as an S3 object '
             'created event'
    )

    args = parser.parse_args()
    audit(args.format, args.output)
