FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/  /app/common/
COPY orders/  /app/orders/
COPY gateway/ /app/gateway/

# CodeSiteSpanProcessor emits paths relative to this, so span attributes read as
# "orders/repository.py" -- directly comparable to git's output.
ENV SHOPDEMO_SOURCE_ROOT=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "orders.main:app", "--host", "0.0.0.0", "--port", "8000"]
