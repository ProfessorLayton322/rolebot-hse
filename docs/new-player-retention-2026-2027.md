# New-player attendance and returns, 2026–2027

Snapshot analysed on 8 November 2026 from the
[player attendance spreadsheet, tab `2026-2027`](https://docs.google.com/spreadsheets/d/1NaWqFbeD9oHfvxU-4TFfQaV1b92N-Icx/edit?gid=94150178#gid=94150178).
The last recorded game is 7 November 2026. These are season-to-date results.

A newcomer has **zero games in column C at the start of the season** and at
least one attendance mark in the season. Their cohort is their first attended
game. A return means attendance on a **strictly later date**, through the last
recorded game; it does not imply continued attendance after that observation.
Existing players attending their first game of this season are excluded from
newcomer counts.

## Results

| First game | Date | Unique attendees | Newcomers | Returned later | Return rate | Later events available |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Лесной мир | 19.09.2026 | 20 | 17 | 10 | 58.8% | 8 |
| Бэнг! | 19.09.2026 | 10 | 5 | 1 | 20.0% | 8 |
| МКС | 26.09.2026 | 15 | 5 | 2 | 40.0% | 6 |
| СЕРП | 26.09.2026 | 12 | 7 | 0 | 0.0% | 6 |
| Сад расходящихся троп | 03.10.2026 | 12 | 4 | 2 | 50.0% | 5 |
| All six games on 10 October | 10.10.2026 | 31 | 11 | 2 | 18.2% | 4 |
| Святые Черти | 17.10.2026 | 27 | 5 | 1 | 20.0% | 3 |
| Сансара Сабзеруза | 24.10.2026 | 9 | 1 | 0 | 0.0% | 2 |
| По образу и подобию | 31.10.2026 | 17 | 4 | 0 | 0.0% | 1 |
| Тайны Дануолла | 07.11.2026 | 21 | 4 | 0 observed | Not yet measurable | 0 |

There are **63 newcomers**, of whom **18 returned** at least once: **28.6%**
of all newcomers. Of the **59** whose first attendance was followed by another
recorded event, **30.5%** returned. These percentages use different denominators.
There are 41 newcomers with no return observed despite later events being
available, and four with no later event yet available. None of these states
establishes that a player has permanently stopped playing.

The season has **100 distinct participants**. Event attendee counts must not be
summed to obtain a season headcount, because returning people appear in several
events. Earlier cohorts also have longer observation windows; the rates above
do not by themselves establish differences in game quality.

## The combined event on 10 October

The six source columns **S:X** form one event. Their 33 attendance marks belong
to **31 distinct people**; two attended two games each. A person counts once
within the combined event. Multiple games on that date do not count as a
subsequent return. The combined cohort has **11 newcomers**, of whom **two**
attended a later event, giving **18.2%**.

All other games remain separate. No newcomer has an ambiguous first game
across multiple games on another date in this snapshot.

## Reconciliation and deliverable

The exported tab has 810 player rows, **2–811**; rows 812–815 are summaries.
Of those players, 744 have games before the season and 66 have a zero starting
count. Of the latter, 63 attended at least one season game and three did not.
The attendance cells in **N:AB** contain only numeric `1` or blanks. There are
no duplicate names after whitespace and case normalization.

The calculated 63 newcomers and 100 season participants match the source
footer. All 15 individual game headcounts match their footer values, and all
810 current lifetime totals equal the starting count plus the attendance marks.
The downloaded XLSX snapshot has SHA-256
`8674e50f27dc0ea6969cbca387f00f3d3078cc47864f55bb42801a9fb5ace46a`.

The requested local workbook, `new_player_retention_2026-2027.xlsx`, contains
a Russian summary and chart, the 63 newcomers with their first and subsequent
events, an event mapping, the source attendance rows, and calculation notes.
Its summary formulas include cached results for viewing before recalculation.
It can be uploaded to Google Drive and opened in Google Sheets. It is a fixed
snapshot, without an automatic connection to the source spreadsheet. Personal
player details and the generated workbook are excluded from this repository
change; XLSX artifacts remain covered by the existing ignore rule.
