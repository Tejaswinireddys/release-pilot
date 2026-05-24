FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY scenarios ./scenarios
COPY policies ./policies
COPY config ./config
CMD ["python", "src/tools/aws_mock_server.py"]
