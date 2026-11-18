FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mrt_route ./mrt_route
COPY webapp ./webapp

EXPOSE 8000
# One process (solves fan out to a process pool), several threads for concurrent requests,
# no request timeout because "prove optimality" jobs can run for minutes.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "0", "-b", "0.0.0.0:8000", "webapp.app:create_app()"]
