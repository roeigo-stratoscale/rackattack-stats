#!/bin/sh
RAP_URI='rackattack-provider' RACKATTACK_PROVIDER=tcp://${RAP_URI}:1014@@amqp://guest:guest@${RAP_URI}:1013/%2F@@http://${RAP_URI}:1016 UPSETO_JOIN_PYTHON_NAMESPACES=yes PYTHONPATH=`dirname '$0'`/py python ~/work/rackattack-stats/py/rackattack/stats/main_allocation_stats.py
