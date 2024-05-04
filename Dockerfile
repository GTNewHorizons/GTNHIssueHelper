FROM python:3.10

RUN pip install "github-action-utils~=1.1.0" "requests<3" "urllib3<2" "git+https://github.com/GTNewHorizons/DreamAssemblerXXL.git"

WORKDIR /app
COPY entrypoint.py /app/main.py

CMD ["python", "main.py"]