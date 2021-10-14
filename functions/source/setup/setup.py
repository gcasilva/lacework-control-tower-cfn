#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
# PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
import random
import string

import boto3
import json
import logging
import os
import time

import requests
import urllib3
from crhelper import CfnResource

SUCCESS = "SUCCESS"
FAILED = "FAILED"

http = urllib3.PoolManager()

LOGLEVEL = os.environ.get('LOGLEVEL', logging.INFO)
logger = logging.getLogger()
logger.setLevel(LOGLEVEL)
logging.getLogger("boto3").setLevel(logging.CRITICAL)
logging.getLogger("botocore").setLevel(logging.CRITICAL)
session = boto3.Session()

helper = CfnResource(json_logging=False, log_level="INFO", boto_level="CRITICAL", sleep_on_delete=15)


def lambda_handler(event, context):
    logger.info("setup.lambda_handler called.")
    logger.info(json.dumps(event))
    try:
        if "RequestType" in event: helper(event, context)
    except Exception as e:
        helper.init_failure(e)


@helper.create  # crhelper methods to create the stack set if needed and create stack instances
@helper.update
def create(event, context):
    logger.info("setup.create called.")
    logger.info(json.dumps(event))
    first_launch = False
    try:
        cloud_formation_client = session.client("cloudformation")
        stack_set_name = os.environ['stack_set_name']
        stack_set_url = os.environ['stack_set_url']
        lacework_account_name = os.environ['lacework_account_name']
        lacework_api_credentials = os.environ['lacework_api_credentials']

        access_token = setup_initial_access_token(lacework_account_name, lacework_api_credentials)
        if access_token is None:
            message = "Unable to get Lacework access token."
            logger.error(message)
            send_cfn_response(event, context, FAILED, {"Message": message})
            return None

        lacework_stack_set_sns = os.environ['lacework_stack_set_sns']
        create_trail = os.environ['create_trail']
        trail_log_prefix = os.environ['trail_log_prefix']
        existing_trail_s3_bucket_name = os.environ['existing_trail_s3_bucket_name']
        existing_trail_topic_arn = os.environ['existing_trail_topic_arn']
        management_account_id = context.invoked_function_arn.split(":")[4]
        region_name = context.invoked_function_arn.split(":")[3]
        external_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=7))
        cloud_formation_client.describe_stack_set(StackSetName=stack_set_name)
        logger.info("Stack set {} already exist".format(stack_set_name))
        helper.Data.update({"result": stack_set_name})

    except Exception as describeException:
        logger.info("Stack set {} does not exist, creating it now.".format(stack_set_name))
        cloud_formation_client.create_stack_set(
            StackSetName=stack_set_name,
            Description="Lacework's cloud-native threat detection, compliance, behavioral anomaly detection, "
                        "and automated AWS security monitoring.",
            TemplateURL=stack_set_url,
            Parameters=[
                {
                    "ParameterKey": "ResourceNamePrefix",
                    "ParameterValue": lacework_account_name,
                    "UsePreviousValue": False,
                    "ResolvedValue": "string"
                },
                {
                    "ParameterKey": "ExternalID",
                    "ParameterValue": external_id,
                    "UsePreviousValue": False,
                    "ResolvedValue": "string"
                },
                {
                    "ParameterKey": "AccessToken",
                    "ParameterValue": access_token,
                    "UsePreviousValue": False,
                    "ResolvedValue": "string"
                },
                {
                    "ParameterKey": "CreateTrail",
                    "ParameterValue": create_trail,
                    "UsePreviousValue": False,
                    "ResolvedValue": "string"
                },
                {
                    "ParameterKey": "NewTrailLogFilePrefix",
                    "ParameterValue": trail_log_prefix,
                    "UsePreviousValue": False,
                    "ResolvedValue": "string"
                },
                {
                    "ParameterKey": "ExistingTrailBucketName",
                    "ParameterValue": existing_trail_s3_bucket_name,
                    "UsePreviousValue": False,
                    "ResolvedValue": "string"
                },
                {
                    "ParameterKey": "ExistingTrailTopicArn",
                    "ParameterValue": existing_trail_topic_arn,
                    "UsePreviousValue": False,
                    "ResolvedValue": "string"
                }
            ],
            Capabilities=[
                "CAPABILITY_NAMED_IAM"
            ],
            AdministrationRoleARN="arn:aws:iam::" + management_account_id + ":role/service-role"
                                                                            "/AWSControlTowerStackSetRole",
            ExecutionRoleName="AWSControlTowerExecution")

        try:
            result = cloud_formation_client.describe_stack_set(StackSetName=stack_set_name)
            first_launch = True
            logger.info("StackSet {} deployed".format(stack_set_name))
        except cloud_formation_client.exceptions.StackSetNotFoundException as describeException:
            message = "Exception getting new stack set, {}".format(describeException)
            logger.error(message)
            send_cfn_response(event, context, FAILED, {"Message": message})
            raise describeException

        try:
            if first_launch and len(os.environ['account_list']) > 0:
                logger.info("New accounts : {}".format(os.environ['account_list']))
                account_list = os.environ['account_list'].split(",")
                sns_client = session.client("sns")
                message_body = {stack_set_name: {"target_accounts": account_list, "target_regions": [region_name]}}
                try:
                    sns_response = sns_client.publish(
                        TopicArn=lacework_stack_set_sns,
                        Message=json.dumps(message_body))

                    logger.info("Queued for stackset instance creation: {}".format(sns_response))
                except Exception as snsException:
                    logger.error("Failed to send queue for stackset instance creation: {}".format(snsException))
            else:
                logger.info("No additional stackset instances requested")
        except Exception as create_exception:
            message = "Exception creating stack instance with {}".format(create_exception)
            logger.error(message)
            send_cfn_response(event, context, FAILED, {"Message": message})
            raise create_exception

        helper.Data.update({"result": stack_set_name})

    if not helper.Data.get("result"):
        message = "Error occurred during solution setup"
        send_cfn_response(event, context, FAILED, {"Message": message})
        raise ValueError("Error occurred during solution setup")

    send_cfn_response(event, context, SUCCESS, {})
    return None


@helper.delete  # crhelper method to delete stack set and stack instances
def delete(event, context):
    logger.info("setup.delete called.")
    delete_wait_time = (int(context.get_remaining_time_in_millis()) - 100) / 1000
    delete_sleep_time = 30
    try:
        stack_set_name = os.environ['stack_set_name']
        stack_set_url = os.environ['stack_set_url']
        management_account_id = context.invoked_function_arn.split(":")[4]
        cloud_formation_client = session.client("cloudformation")
        region_name = context.invoked_function_arn.split(":")[3]
        cloud_formation_client.describe_stack_set(StackSetName=stack_set_name)
        logger.info("Stack set {} exist".format(stack_set_name))

        paginator = cloud_formation_client.get_paginator("list_stack_instances")
        page_iterator = paginator.paginate(StackSetName=stack_set_name)
        stack_set_list = []
        account_list = []
        region_list = []
        for page in page_iterator:
            if "Summaries" in page:
                stack_set_list.extend(page['Summaries'])
        for instance in stack_set_list:
            account_list.append(instance['Account'])
            region_list.append(instance['Region'])
        region_list = list(set(region_list))
        account_list = list(set(account_list))
        logger.info("StackSet instances found in region(s): {}".format(region_list))
        logger.info("StackSet instances found in account(s): {}".format(account_list))

        try:
            if len(account_list) > 0:
                response = cloud_formation_client.delete_stack_instances(
                    StackSetName=stack_set_name,
                    Accounts=account_list,
                    Regions=region_list,
                    RetainStacks=False)
                logger.info(response)

                status = cloud_formation_client.describe_stack_set_operation(
                    StackSetName=stack_set_name,
                    OperationId=response['OperationId'])

                while status['StackSetOperation']['Status'] == "RUNNING" and delete_wait_time > 0:
                    time.sleep(delete_sleep_time)
                    delete_wait_time = delete_wait_time - delete_sleep_time
                    status = cloud_formation_client.describe_stack_set_operation(
                        StackSetName=stack_set_name,
                        OperationId=response['OperationId'])
                    logger.info("StackSet instance delete status {}".format(status))

            try:
                response = cloud_formation_client.delete_stack_set(StackSetName=stack_set_name)
                logger.info("StackSet template delete status {}".format(response))
            except Exception as stackSetException:
                logger.warning("Problem occurred while deleting, StackSet still exist : {}".format(stackSetException))

        except Exception as describeException:
            logger.error(describeException)

    except Exception as describeException:
        logger.error(describeException)
        return None

    send_cfn_response(event, context, SUCCESS, {})
    return None


def send_cfn_response(event, context, response_status, response_data, physical_resource_id=None, no_echo=False,
                      reason=None):
    response_url = event['ResponseURL']

    logger.info(response_url)

    response_body = {
        'Status': response_status,
        'Reason': reason or "See the details in CloudWatch Log Stream: {}".format(context.log_stream_name),
        'PhysicalResourceId': physical_resource_id or context.log_stream_name,
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'NoEcho': no_echo,
        'Data': response_data
    }

    json_response_body = json.dumps(response_body)

    logger.info("Response body: {}".format(json_response_body))

    headers = {
        'content-type': '',
        'content-length': str(len(json_response_body))
    }

    try:
        response = http.request('PUT', response_url, headers=headers, body=json_response_body)
        logger.info("Status code: {}".format(response.status))

    except Exception as e:
        logger.error("send_cfn_response error {}".format(e))


def setup_initial_access_token(lacework_account_name, lacework_api_credentials):
    secret_client = session.client('secretsmanager')
    try:
        secret_response = secret_client.get_secret_value(
            SecretId=lacework_api_credentials
        )
        if 'SecretString' not in secret_response:
            logger.error("SecretString not found in {}".format(lacework_api_credentials))
            return None

        secret_string_dict = json.loads(secret_response['SecretString'])
        access_key_id = secret_string_dict['AccessKeyID']
        secret_key = secret_string_dict['SecretKey']

        request_payload = '''
        {{
            "keyId": "{}", 
            "expiryTime": 86400
        }}
        '''.format(access_key_id)
        logger.debug('Generate access key payload : {}'.format(json.dumps(request_payload)))

        response = requests.post("https://"+lacework_account_name + ".lacework.net/api/v2/access/tokens",
                                 headers={'X-LW-UAKS': secret_key, 'content-type': 'application/json'},
                                 verify=True, data=request_payload)
        logger.info('API response code : {}'.format(response.status_code))
        logger.debug('API response : {}'.format(response.text))
        if response.status_code == 201:
            payload_response = response.json()
            expires_at = payload_response['expiresAt']
            token = payload_response['token']
            secret_string_dict['AccessToken'] = token
            secret_string_dict['TokenExpiry'] = expires_at
            secret_client.update_secret(SecretId=lacework_api_credentials, SecretString=json.dumps(secret_string_dict))
            return token
        else:
            logger.error("Generate access key failure {} {}".format(response.status_code, response.text))
            return None
    except Exception as e:
        logger.error("Error setting up initial access token {}".format(e))
        return None
