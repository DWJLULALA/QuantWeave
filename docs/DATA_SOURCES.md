# Data sources

These are the connections used by the live research environment. **The local demo only reads frozen snapshots.** Its update buttons run simulated jobs and do not contact these providers.

| Data | Current connection | Code entry points | Checks required |
|---|---|---|---|
| Daily stocks and trading calendar | Tushare and upstream research scripts | `update_data.sh`, `update_latest.py`; upstream `fetch_official_close.py` | Supply authorization and upstream scripts; verify trading dates and the latest completed close |
| 30-minute stocks | East Money K-line API | `update_intraday_market_parallel.py`, called by `update_latest.py` | `scale=30`, unadjusted prices; convert lots to shares; preserve failure records and existing data |
| Daily broad-market indices | SSE CSIP series API | `trend_analysis.py` | Index codes, date coverage, missing rows and duplicates |
| Daily Shenwan industries | SWS Research index API | `trend_analysis.py` | Classification and constituent versions, missing-date repairs and source conflicts |
| Auxiliary intraday indices | Tencent quotes | `trend_analysis.py` | Code mapping, available sessions and history length |

The repository also contains a Tushare-compatible interface and Tencent/Sina repair scripts. They are separate adapters, **not a guarantee of automatic failover across every provider**. Configure and verify access, quotas and availability for your environment. The project is not affiliated with these providers.

## Update sequence

1. Read the trading calendar and identify the latest fully closed daily session.
2. Truncate intraday data to the latest completed 30-minute boundary; exclude unfinished bars.
3. Fetch increments, deduplicate and normalize dates and volume units.
4. Check gaps, source conflicts and failed items before producing research snapshots.
5. Inspect job logs and artifacts to confirm actual coverage. A completed process does not mean every stock updated successfully.

Official references: [Tushare daily stocks](https://tushare.pro/document/2?doc_id=27) and [trading calendar](https://tushare.pro/document/2?doc_id=26). The source files above define the application's actual integrations.
