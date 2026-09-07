# Player and master statistics workbooks

## Player attendance

The supplied `Копия Сводная статистика посещений.xlsx` has 12 sheets. Seasons are ordered newest first, alongside older helper lists and a special-awards sheet. The latest season is `2025-2026`: 747 player rows (2–748), game columns N–AN, and four summary rows (749–752). Formatting extends well below the real data; worksheet dimensions must not be used as the player count. The editor locates the latest year-pair sheet by its name, not by whichever tab happens to be active.

| Columns | Contents | Existing appearance/behaviour |
| --- | --- | --- |
| A | Surname and name | Lavender fill. A conditional pink fill flags a reached milestone whose corresponding award flag is FALSE. The current rule stops at row 675, omitting later players. |
| B | Lifetime game count | Pale green fill; formula `=C2+SUM(N2:AP2)` and analogous row formulas. Red bold text flags 4, 9, 14, 19, 24, 29, 34, 39, 44 and 49 games. The 19/24 rules are text searches, so they can also match larger numbers containing those digits. |
| C | Total brought forward | Peach fill. Values are carried totals; the latest sheet still labels this `Игр на 01.09.2024`, which is stale. |
| D–M | 5, 10, …, 50 game milestones | Peach fill. These are stored TRUE/FALSE flags (some later rows are blank), not formulas derived from the total. They are carried forward unchanged, rather than automatically checked. |
| N onward | Games | `1` means attendance and triggers a green fill. Blank cells have a yellow conditional fill over much of the range. Some early nonblank cells have another pale-green rule. |

Headers and totals are generally lavender. A few game headers/totals have manually applied red or green fills. There is no legend establishing their meaning, so the implementation does not infer attendance or any extra state from those colours.

The current player's lifetime formula adds the carried total to the season's game cells. One attendance cell, P78, contains `❌`; Excel's SUM ignores that text. Computing all 747 totals from column C and the game cells exactly reproduces the supplied workbook's cached B values. The hardcoded AP endpoint would eventually omit games appended after AP; edited sheets instead use the actual final game column.

The footer counts players with any lifetime games, players with games before the season, total attendance per game, season participants, newcomers, and active season players (more than three games that season). Some exported array formulas refer to AY2, and the first footer label itself contains an unrelated array formula with an outdated range. On an edited sheet, the four summaries are rebuilt as `Всего`, `Участники сезона`, `Новички`, and `Активные игроки сезона`, with COUNTIF/SUM/SUMPRODUCT formulas and fresh cached values. Their ranges expand when players are added. The intended newcomers calculation remains lifetime-positive players minus carried-total-positive players.

Older sheets contain exported Google Sheets formulas, including DUMMYFUNCTION wrappers around IMPORTRANGE operations. A full openpyxl load/save drops formula caches across the workbook, leaving those formulas unusable without their original external context. The implementation therefore edits the XLSX ZIP/XML parts directly. Historical worksheet parts, shared strings, styles and unrelated resources remain byte-identical; new or edited totals receive both formulas and numeric cached results. Each result is uploaded as a new revision of the same permanent Google Drive file ID.

New seasons retain A–M, carry the previous final totals into C, preserve award flags, update C's date, and create no game columns or attendance marks. Totals initially use `=C2`. Attendance and milestone formatting ranges follow new rows/columns; existing differential colours and milestone expressions are retained, including the original 19/24 text-search semantics. New players start with zero carried games and false milestone flags. Names and game titles are written as literal strings, even when they begin with `=`.

The supported input layout is the current A–M schema. Older layouts without the 5–50 milestone columns are rejected rather than interpreted as a current table. A missing or invalid source produces an admin error without changing the working workbook. Formula cells needed for carried totals must have usable cached numeric results; the bot cannot execute arbitrary Excel or Google Sheets formulas.

## Master participation

The supplied `master_statistics.xlsx` has nine seasonal sheets. The latest source season is `2025-2026`: 101 master rows (2–102), fixed columns A–L, game columns M–AK, and three summary rows (103–105). Formatting extends to row 1024, so the editor finds the real master boundary from the names and known summaries rather than using worksheet dimensions.

| Columns | Contents | Preserved appearance/behaviour |
| --- | --- | --- |
| A | Master name | Lavender fill. A conditional pink fill flags a reached milestone whose corresponding award flag is FALSE. |
| B | Participation in the current season | Pale yellow fill and formula `=D2-C2`. |
| C | Lifetime games brought forward | Existing numeric value and per-row style. |
| D | Current lifetime game count | Formula `=C2+SUM(M2:AK2)` and analogous row formulas. Existing red milestone text rules are retained. |
| E–L | 1, 5, 10, …, 35 game milestones | Existing TRUE/FALSE award flags, carried unchanged into a new season. New masters start with FALSE flags. |
| M onward | Games | `1` marks participation; the source green/nonblank and yellow/blank conditional-formatting rules expand with new masters and games. |

A new season copies the latest master list and fixed-column styling into a newly inserted seasonal sheet, carries each final lifetime total into C, resets B to zero through `=D-C`, and removes the old season's game columns. Adding the first game recreates the lifetime formulas from M through the actual last game column. The summaries remain `Всего`, `Участники сезона`, and `Новички`; their formulas and cached values are rebuilt with expanded row and game ranges, using the source summary styles.

As with the player workbook, the editor changes OOXML parts directly. Existing worksheet parts, shared strings, styles, drawings, comments, and formula caches remain byte-identical when a new season is added. Names and game titles are stored as literal strings, name matching is normalized, and an existing current-season game header makes the edit a no-op.
