FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal: no Chromium needed because browser.py connects to
# Bright Data over CDP rather than launching a local browser.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
