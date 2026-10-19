FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
ENV PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir playwright==1.63.0
COPY goodstack_batch_verify.py /app/goodstack_batch_verify.py
ENTRYPOINT ["python", "/app/goodstack_batch_verify.py"]
