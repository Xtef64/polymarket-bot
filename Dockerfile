FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Les variables d'environnement (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, etc.)
# sont injectées par Railway au runtime — aucune valeur sensible ici.
CMD ["python", "-u", "main.py"]
