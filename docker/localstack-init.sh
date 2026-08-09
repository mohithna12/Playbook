#!/bin/bash
# Create S3 buckets for local development
awslocal s3 mb s3://playbook-raw
awslocal s3 mb s3://playbook-models
awslocal s3 mb s3://playbook-features
echo "LocalStack S3 buckets created."
