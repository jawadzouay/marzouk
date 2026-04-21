FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Keep cwd at /app so `python backend/run.py` matches railway.json/Procfile.
# If WORKDIR were /app/backend, Railway's startCommand would resolve to
# /app/backend/backend/run.py and the container would crash on boot.

EXPOSE 8000

CMD ["python", "backend/run.py"]
