FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app/router

ENV PYTHONPATH=/app
EXPOSE 4100

CMD ["uvicorn", "router.main:app", "--host", "0.0.0.0", "--port", "4100"]
