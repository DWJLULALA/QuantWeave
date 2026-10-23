FROM python:3.12-slim

WORKDIR /app
COPY requirements-demo.txt ./
RUN pip install --no-cache-dir -r requirements-demo.txt
COPY . .

EXPOSE 7072
CMD ["python", "demo_server.py", "--host", "0.0.0.0", "--port", "7072"]
