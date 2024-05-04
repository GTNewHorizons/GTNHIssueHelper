FROM python:3.10

RUN pip install "github-action-utils~=1.1.0" "requests<3" "urllib3<2"  \
    "git+https://github.com/GTNewHorizons/DreamAssemblerXXL.git" \
    "packaging==21.3"

COPY entrypoint.py log_utils.py /app/
ENV PYTHONPATH=/app:$PYTHONPATH

CMD ["python", "/app/main.py"]
