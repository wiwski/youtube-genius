#!/bin/bash
celery worker -A application.celery --loglevel=INFO
