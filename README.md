# FX-dashboard
하나은행 매매기준율(최초 고시) 기준 USD·EUR·GBP·CNY 환율 대시보드 (모바일용)

- 최신 고시 환율 / 당월 평균 / 연간 평균 (하나은행 평균환율 메뉴 값)
- 수집: GitHub Actions, 평일 09:30 KST → `docs/data/rates.json`
- 첫 실행 시 올해 1/1부터 일별 최초 고시 환율을 채움
- 화면: `docs/index.html` (GitHub Pages)

## 설정
1. Settings → Pages → Source: `Deploy from a branch`, Branch: `main` / `/docs`
2. Settings → Actions → General → Workflow permissions: `Read and write permissions`
3. Actions → "Update FX rates" → Run workflow 로 첫 실행

실행 결과의 `hana-raw-responses` 아티팩트에 하나은행 원본 응답이 저장됩니다(파싱 점검용).
