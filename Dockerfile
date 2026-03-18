FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the FastEmbed model at build time
# so there's no HuggingFace call on Cloud Run cold starts
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')"

COPY . .

# Cloud Run injects PORT (default 8080); app reads os.getenv("PORT", "8004")
EXPOSE 8080

CMD ["python", "main.py", "--mode", "server"]
