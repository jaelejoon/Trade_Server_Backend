# Windows PowerShell에서 실행
# 목적: nginx 설정 문법 검사와 컨테이너 상태 확인

docker compose -f docker-compose.prod.yml config
if ($LASTEXITCODE -ne 0) {
    Write-Error "docker-compose.prod.yml 설정에 문제가 있습니다."
    exit 1
}

docker compose -f docker-compose.prod.yml run --rm nginx nginx -t
if ($LASTEXITCODE -ne 0) {
    Write-Error "nginx 설정 문법에 문제가 있습니다."
    exit 1
}

Write-Host "OK: docker compose / nginx 설정 검사가 끝났습니다."
