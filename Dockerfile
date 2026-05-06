FROM python:3.12-slim

WORKDIR /app

# Cached deps layer — installs change rarely
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py .

# No user/secret baked into the image — supply at runtime via -e or --env-file
ENV PYTHONUNBUFFERED=1

CMD ["python", "agent.py"]
