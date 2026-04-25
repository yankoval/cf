import os
import json
import pytest
import boto3
from moto import mock_aws
from index import handler

@pytest.fixture
def aws_credentials():
    os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
    os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'
    os.environ['AWS_SECURITY_TOKEN'] = 'testing'
    os.environ['AWS_SESSION_TOKEN'] = 'testing'
    os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
    os.environ['AWS_REGION'] = 'us-east-1'
    os.environ['S3_ACCESS_KEY_ID'] = 'testing'
    os.environ['S3_SECRET_ACCESS_KEY'] = 'testing'
    os.environ['BUCKET'] = 'dest-bucket'
    os.environ['S3_ENDPOINT'] = '' # Use default for moto

@mock_aws
def test_handler_new_structure(aws_credentials):
    s3 = boto3.client('s3', region_name='us-east-1')
    s3.create_bucket(Bucket='src-bucket')
    s3.create_bucket(Bucket='dest-bucket')

    object_id = 'Задания/0e3dd66c-6e7a-4d12-986e-a6917940b0ea.json'
    content = {
        "Article": "7140-77-002",
        "Gtin": "4610117651505",
        "Quantity": "1",
        "PasportData": {
            "Product_PackQty": "6",
            "Product_name_part1": "WB Стойкая крем-краска"
        }
    }
    s3.put_object(Bucket='src-bucket', Key=object_id, Body=json.dumps(content))

    event = {
        'messages': [
            {
                'details': {
                    'bucket_id': 'src-bucket',
                    'object_id': object_id
                }
            }
        ]
    }

    response = handler(event, None)
    assert response['statusCode'] == 200

    # Verify processed object in destination bucket
    # Expected filename: 0e3dd66c-6e7a-4d12-986e-a6917940b0ea-04610117651505.json
    dest_key = 'processed/0e3dd66c-6e7a-4d12-986e-a6917940b0ea-04610117651505.json'
    dest_obj = s3.get_object(Bucket='dest-bucket', Key=dest_key)
    dest_content = json.loads(dest_obj['Body'].read().decode('utf-8'))

    assert dest_content['id'] == '0e3dd66c-6e7a-4d12-986e-a6917940b0ea'
    assert dest_content['gtin'] == '04610117651505'
    assert dest_content['numРacksInBox'] == '6'
    assert dest_content['boxLabelFields'] == 'WB Стойкая крем-краска'
    assert 'task-export-signed-link' in dest_content
