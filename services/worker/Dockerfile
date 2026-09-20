FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
COPY ../api/requirements.txt ./api-requirements.txt

RUN pip install --no-cache-dir -r api-requirements.txt

COPY ../api/*.py ./
COPY worker.py ./

CMD ["python", "supervisor.py"]
