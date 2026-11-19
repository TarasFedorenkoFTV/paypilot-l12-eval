#!/bin/bash
curl -X "$1" "localhost:8000$2" -H "Content-Type: application/json" -d "$3"