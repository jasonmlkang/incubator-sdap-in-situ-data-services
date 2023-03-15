# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import json
from typing import Union

from parquet_flask.aws.aws_secret_manager import AwsSecretManager
from parquet_flask.authenticator.authenticator_abstract import AuthenticatorAbstract


class AuthenticatorAwsSecretManager(AuthenticatorAbstract):
    __auth_cred_key = 'auth_cred'

    def __init__(self) -> None:
        super().__init__()
        self.__aws_ssm = AwsSecretManager()
        self.__token = ''

    def get_auth_credentials(self, cred_name: str) -> dict:
        try:
            token_str = self.__aws_ssm.get_secret(cred_name)
        except Exception as e:
            raise ValueError(f'error while retrieving secret manager value: {cred_name}. cause: {str(e)}')
        secret_json = json.loads(token_str)[self.__auth_cred_key]  # TODO check this
        if self.__auth_cred_key not in secret_json:
            raise ValueError(f'invalid json. missing {self.__auth_cred_key}. {secret_json}')
        self.__token = secret_json[self.__auth_cred_key]
        return secret_json

    def authenticate(self, input_auth_cred: dict) -> Union[str, None]:
        if 'Authorization' not in input_auth_cred:
            return f'missing key: Authorization'
        try:
            input_auth_value = base64.standard_b64decode(input_auth_cred['Authorization'].encode()).decode()
        except:
            return f'unable to base64 decode the value from Authorization'
        if input_auth_value != self.__token:
            return f'mismatch incoming base64 token vs existing token'
        return None
