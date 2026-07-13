# AFOS Production Validation Report

- **Mode:** full
- **Generated:** 2026-07-13 04:46:52
- **Total execution time:** 2025.76s
- **Checks:** 50 passed / 1 failed / 4 skipped (of 55)
- **Health score:** 98.0%
- **Result:** FAIL

| Category | Check | Status | Time (s) | Detail / Error |
|---|---|---|---|---|
| environment | Python environment | PASS | 0.00 | Python 3.13.14 at C:\CAI-Operating-System\.venv\Scripts\python.exe |
| environment | Virtual environment | PASS | 0.00 | active: C:\CAI-Operating-System\.venv (project .venv) |
| environment | Required packages | PASS | 0.13 | 44 pinned packages present |
| environment | pip check | PASS | 0.98 | no broken requirements |
| environment | Git status | PASS | 0.35 | branch=phase-1, 13 uncommitted change(s) |
| environment | LangGraph verification | PASS | 1.81 | PASS - 9/9 checks succeeded |
| providers | API key verification | PASS | 0.00 | configured: GOOGLE_API_KEY, TAVILY_API_KEY |
| providers | Gemini verification | SKIP | 7.72 | Skipped (Provider Unavailable): Gemini HTTP 429: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini- |
| providers | Tavily verification | PASS | 2.48 | 1 result(s), e.g. https://www.researchgate.net/figure/Examples-of-AM-AFOs-produced-by-a-Faustini-et-al-15-b-Schrank-et-al-16-c_fig2_283636848 |
| providers | Ollama verification | PASS | 2.76 | 1 model(s): qwen3:8b |
| providers | Brave verification | SKIP | 0.00 | Skipped (Provider Unavailable): BRAVE_API_KEY not configured |
| providers | SearXNG verification | SKIP | 0.00 | Skipped (Provider Unavailable): SearXNG not reachable at http://localhost:8080 (not running) |
| storage | Database verification | PASS | 5.46 | knowledge_conn OK, quick_check=ok, afos.db=104.1MB |
| storage | Workspace verification | PASS | 0.01 | create/get round-trip OK (probe project a7477669-ecff-43b3-9a37-5ab304308d7b) |
| storage | Memory verification | PASS | 0.00 | working-memory checkpointer OK, vector store schema OK |
| storage | Vector embeddings verification | SKIP | 0.00 | Skipped (Provider Unavailable): OPENAI_API_KEY not configured - VectorStore's default embed_fn requires it (this environment uses Google/Tavily; embeddings are an optional tier) |
| pipeline | Founder Orchestrator | PASS | 8.07 | status=completed |
| pipeline | Research Pipeline | PASS | 6.40 | status=completed |
| pipeline | Decision Engine | PASS | 6.31 | status=completed |
| pipeline | MVP Planner | PASS | 6.45 | status=completed |
| pipeline | AI Builder | PASS | 97.38 | status=build_failed |
| pipeline | Deployment Pipeline | PASS | 97.49 | status=skipped |
| pipeline | Growth Pipeline | PASS | 97.75 | status=skipped |
| pipeline | Founder Dashboard | PASS | 469.62 | status=completed |
| storage | Workspace verification (deep) | PASS | 0.00 | create/list/archive/unarchive/clone round-trip OK (007fa43a-5bda-4f2e-bfc1-9845b8799492 -> 4739ded2-d184-4c91-81d3-b7bb115deeb1) |
| storage | Memory verification (deep) | PASS | 0.00 | recorded+verified all 5 memory kinds for a probe venture |
| regression | smoke_test_phase0.py | PASS | 49.88 |  |
| regression | smoke_test_phase1_browser_agent.py | PASS | 10.67 |  |
| regression | smoke_test_phase1_competitor_intelligence.py | PASS | 14.41 |  |
| regression | smoke_test_phase1_idea_validation.py | PASS | 14.69 |  |
| regression | smoke_test_phase1_market_intelligence.py | PASS | 14.70 |  |
| regression | smoke_test_phase1_opportunity_detection.py | PASS | 14.38 |  |
| regression | smoke_test_phase1_research_supervisor.py | PASS | 10.19 |  |
| regression | smoke_test_phase1_search_agent.py | PASS | 8.37 |  |
| regression | smoke_test_phase1_trend_intelligence.py | PASS | 14.75 |  |
| regression | smoke_test_phase2_build_runner.py | PASS | 5.37 |  |
| regression | smoke_test_phase2_claude_code.py | PASS | 0.65 |  |
| regression | smoke_test_phase2_debug_retry.py | PASS | 3.19 |  |
| regression | smoke_test_phase2_file_editor.py | PASS | 4.78 |  |
| regression | smoke_test_phase2_git_agent.py | PASS | 4.74 |  |
| regression | smoke_test_phase2_loop_controller.py | PASS | 4.53 |  |
| regression | smoke_test_phase2_terminal.py | PASS | 5.18 |  |
| regression | smoke_test_phase3_chroma.py | PASS | 5.28 |  |
| regression | smoke_test_phase3_crawl4ai.py | PASS | 4.83 |  |
| regression | smoke_test_phase3_crm.py | PASS | 11.67 |  |
| regression | smoke_test_phase3_docker.py | PASS | 5.30 |  |
| regression | smoke_test_phase3_langsmith.py | PASS | 9.16 |  |
| regression | smoke_test_phase3_mcp.py | PASS | 13.16 |  |
| regression | smoke_test_phase3_n8n.py | PASS | 20.87 |  |
| regression | smoke_test_phase3_ollama.py | PASS | 116.92 |  |
| regression | smoke_test_phase3_playwright.py | PASS | 8.60 |  |
| regression | smoke_test_phase3_postgres.py | PASS | 5.00 |  |
| regression | smoke_test_phase3_searxng.py | PASS | 18.26 |  |
| regression | smoke_test_phase3_voice.py | PASS | 10.21 |  |
| regression | smoke_test_phase5_workspace.py | FAIL | 534.37 | == 12. pip check == [PASS] pip check reports no broken requirements  == 13. git status (only new files, nothing modified) == [FAIL] git status has no modified/deleted files (beyond the known pre-exist |
