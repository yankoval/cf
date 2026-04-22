import os
import json
import pytest
import boto3
import index
from moto import mock_aws
from index import handler

@pytest.fixture(autouse=True)
def reset_sqs_client():
    """Reset the global sqs_client in index.py before each test."""
    index.sqs_client = None
    yield

@pytest.fixture
def aws_credentials():
    """Mocked AWS Credentials for moto."""
    os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
    os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'
    os.environ['AWS_SECURITY_TOKEN'] = 'testing'
    os.environ['AWS_SESSION_TOKEN'] = 'testing'
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['AWS_REGION'] = 'us-east-1'
    os.environ['S3_ACCESS_KEY_ID'] = 'testing'
    os.environ['S3_SECRET_ACCESS_KEY'] = 'testing'
    # Use standard SQS endpoint so moto can intercept it
    os.environ['SQS_ENDPOINT'] = 'https://sqs.us-east-1.amazonaws.com'
    os.environ['QUEUE_URL'] = 'https://sqs.us-east-1.amazonaws.com/123456789012/test-queue'

@mock_aws
def test_handler_success(aws_credentials):
    # Moto works best with default endpoints
    sqs = boto3.client('sqs', region_name='us-east-1', endpoint_url=os.environ['SQS_ENDPOINT'])
    sqs.create_queue(QueueName='test-queue')

    event = {
        'messages': [
            {
                'details': {
                    'bucket_id': 'test-bucket',
                    'object_id': 'test-object.json'
                }
            }
        ]
    }

    response = handler(event, None)
    assert response['statusCode'] == 200
    assert "Processed 1 messages" in response['body']

    # Verify message in queue
    messages = sqs.receive_message(QueueUrl=os.environ['QUEUE_URL'])['Messages']
    assert len(messages) == 1
    body = json.loads(messages[0]['Body'])
    assert body['bucket'] == 'test-bucket'
    assert body['key'] == 'test-object.json'

@mock_aws
def test_handler_no_queue_url(aws_credentials):
    if 'QUEUE_URL' in os.environ:
        del os.environ['QUEUE_URL']

    event = {'messages': []}
    response = handler(event, None)
    assert response['statusCode'] == 500
    assert 'QUEUE_URL environment variable is required' in response['body']

@mock_aws
def test_handler_missing_details(aws_credentials):
    sqs = boto3.client('sqs', region_name='us-east-1', endpoint_url=os.environ['SQS_ENDPOINT'])
    sqs.create_queue(QueueName='test-queue')

    event = {
        'messages': [
            {
                'details': {
                    'bucket_id': 'test-bucket'
                    # missing object_id
                }
            }
        ]
    }

    response = handler(event, None)
    assert response['statusCode'] == 200
    assert "Processed 0 messages" in response['body']
