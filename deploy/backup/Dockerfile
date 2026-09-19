# The live-backup sidecars: postgres client tools (pg_basebackup for the PITR
# base) plus the AWS CLI (every push to S3). One image serves both sidecars.
FROM postgres:16-alpine
RUN apk add --no-cache aws-cli
