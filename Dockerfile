FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run injects PORT (default 8080); app reads os.getenv("PORT", "8004")
EXPOSE 8080

CMD ["python", "main.py", "--mode", "server"]
