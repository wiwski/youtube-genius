#!/bin/bash
celery worker -A application.celery --loglevel=INFO
#celery -A application.celery worker --loglevel=INFO -n worker1@%h
