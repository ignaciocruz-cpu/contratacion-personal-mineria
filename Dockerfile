FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api.py index.html postulacion.html logo-mineria.png logo-mineria-color.png ./
RUN mkdir -p uploads
EXPOSE 3025
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "3025"]
