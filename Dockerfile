# ATS Job Scraper actor: HTTP-only, no browser needed.
FROM apify/actor-python:3.12

COPY requirements.txt ./
# Install the Apify SDK explicitly: the base image does not guarantee it.
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

CMD ["python3", "-m", "src"]
