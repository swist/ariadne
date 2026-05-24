FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Railway injects PORT at runtime; default to 8000 for local dev
ENV PORT=8000
EXPOSE 8000

CMD ["python", "server.py"]
