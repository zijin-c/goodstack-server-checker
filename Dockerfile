FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY goodstack_batch_verify.py /app/goodstack_batch_verify.py
ENTRYPOINT ["python", "/app/goodstack_batch_verify.py"]
