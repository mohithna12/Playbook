#!/bin/bash
# Create S3 buckets for local development
awslocal s3 mb s3://fantasyai-raw
awslocal s3 mb s3://fantasyai-models
awslocal s3 mb s3://fantasyai-features
echo "LocalStack S3 buckets created."
