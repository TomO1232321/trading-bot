# Trendfilter-Trading-Bot (Paper-Trading, kostenlos)

Ein einfacher, **regelbasierter** Trading-Bot in Python. Er benutzt keine KI und keine
kostenpflichtigen Dienste. Er handelt einmal pro US-Handelstag auf einem
**Alpaca-Paper-Konto** (das ist **Spielgeld**, kein echtes Geld), schreibt ein Tagebuch und
vergleicht sich immer mit der einfachsten Alternative: **„einfach SPY kaufen und halten“**.

> ⚠️ **Kein Finanzrat.** Das hier ist ein Lernprojekt. Bevor du jemals echtes Geld einsetzt:
> prüfe, ob Alpaca überhaupt Kunden aus **Dänemark** annimmt, und informiere dich, wie
> **SKAT** Gewinne aus ETFs/Aktien besteuert (in Dänemark gelten für ausländische ETFs
> besondere Regeln, z. B. die Lagerbesteuerung). Der Bot ist absichtlich auf Paper-Trading
> festgelegt und verweigert ein Live-Konto.

---

## 1. Was macht der Bot?

Er beobachtet 6 ETFs (änderbar in `config.json`):

| ETF | Was ist das? |
|---|---|
| SPY | 500 größte US-Firmen |
| QQQ | 100 große US-Technologiefirmen (Nasdaq) |
| IWM | 2000 kleinere US-Firmen |
| EFA | Aktien aus Europa, Japan, Australien |
| TLT | Lang laufende US-Staatsanleihen |
| GLD | Gold |

**Die Regel (Trendfilter):** Ein ETF wird gehalten, wenn

1. der letzte Schlusskurs **über** dem Durchschnitt der letzten 200 Tage liegt **und**
2. der 50-Tage-Durchschnitt **über** dem 200-Tage-Durchschnitt liegt.

Sonst wird er verkauft und das Geld bleibt als Cash liegen. Es werden nur
abgeschlossene Handelstage benutzt (die heutige, noch laufende Tageskerze wird ignoriert).

**Weitere Regeln:**

- Jeder ETF bekommt einen gleich großen Anteil („Slot“), höchstens 20 % des Kontos.
  5 % bleiben immer als Cash-Reserve. Bei 6 ETFs sind das je ca. 15,8 %.
- Nur ganze Aktien, 1 % Sicherheitspuffer beim Kaufen. Erst verkaufen, dann kaufen.
- Jede Position bekommt bei Alpaca einen **Trailing Stop von 10 %**: Fällt der Kurs 10 %
  unter seinen Höchststand seit dem Kauf, verkauft Alpaca automatisch – auch wenn der Bot
  gerade nicht läuft.
- Nach einem ausgelösten Stop wird der ETF **10 Tage lang nicht neu gekauft** (Cooldown).
- Der Bot fasst nur seine eigenen Orders an (sie beginnen mit `tb-`) und nie Positionen,
  die nicht auf der Watchlist stehen. Läuft er zweimal am selben Tag, handelt er nicht doppelt.

## 2. Alpaca-Paper-Schlüssel erstellen

1. Gehe auf <https://alpaca.markets> und erstelle ein kostenloses Konto
   (für Paper-Trading reicht E-Mail-Adresse; keine Einzahlung nötig).
2. Oben links muss **„Paper“** ausgewählt sein (nicht „Live“).
3. Auf der Startseite des Paper-Kontos findest du rechts **„API Keys“** → **„Generate New Keys“**
   (manchmal „Regenerate“).
4. Du bekommst zwei Werte: **Key ID** (beginnt meist mit `PK…`) und **Secret Key**.
   Den Secret Key siehst du **nur einmal** – kopiere ihn sofort in einen Passwortmanager.

> Schlüssel nie in eine Datei im Repository schreiben, nie in Chats posten.

## 3. Schlüssel als GitHub-Secrets eintragen

1. Öffne dein Repository auf github.com.
2. **Settings** (Zahnrad oben) → links **Secrets and variables** → **Actions**.
3. **New repository secret**:
   - Name: `APCA_API_KEY_ID` – Wert: deine Key ID → **Add secret**
4. Nochmal **New repository secret**:
   - Name: `APCA_API_SECRET_KEY` – Wert: dein Secret Key → **Add secret**

Die Namen müssen exakt so geschrieben sein (Großbuchstaben, Unterstriche).

## 4. Workflow starten

1. Im Repository oben auf **Actions** klicken. Falls GitHub fragt, ob Workflows aktiviert
   werden sollen: **„I understand my workflows, go ahead and enable them“**.
2. Links **„Trading-Bot“** wählen → rechts **„Run workflow“**.
3. Im Auswahlmenü **action**:
   - **dry run (show plan, no trades)** – zeigt nur, was der Bot tun *würde*. Ideal für den Anfang.
   - **backtest** – testet die Regeln auf Kursen seit 2018 und vergleicht mit SPY.
   - **trade now** – handelt jetzt sofort (nur wenn die US-Börse offen ist).
4. Grüner Knopf **Run workflow**. Nach ca. 1 Minute auf den Lauf klicken.

**Automatisch:** Montag bis Freitag um 15:07 UTC (= 17:07 dänische Sommerzeit bzw.
16:07 Winterzeit) läuft der Bot von selbst und handelt. An US-Feiertagen notiert er nur
„Markt geschlossen“. Hinweis: GitHub startet geplante Läufe manchmal einige Minuten später –
das ist normal.

## 5. Ergebnisse lesen

- **Im Workflow-Lauf**: Auf der Übersichtsseite eines Laufs (unter „Summary“) steht das
  Tagebuch bzw. die Backtest-Tabelle. Beim Backtest gibt es unten unter **Artifacts** das
  Diagramm `backtest` (ZIP mit `backtest.png`).
- **Im Ordner `journal/`** (wird nach jedem Handelslauf automatisch committet):
  - `JJJJ-MM-TT.md` – Tagebuch des Tages: Kontostand, Signal-Tabelle, Aktionen, Fehler
  - `start.json` – Startkapital und SPY-Kurs vom ersten echten Lauf
  - `history.csv` – eine Zeile pro Tag (kann man in Excel öffnen)
  - Die wichtigste Zeile: *„Seit 2026-10-21: Bot +1,2 % vs. Buy-and-Hold SPY +2,0 % –
    Bot liegt hinten“*
- Im Alpaca-Dashboard (Paper) siehst du alle Orders und Positionen live.

**Signal-Tabelle:** `HOLD` = Trend intakt, halten/kaufen. `OUT` = kein Trend, verkaufen/Cash.
„Cooldown bis …“ = nach Stop-Auslösung gesperrt.

## 6. Realistische Erwartungen

- Ein Trendfilter soll vor allem **große Crashs abmildern** (z. B. 2008, 2022). Er steigt aus,
  *nachdem* ein Abwärtstrend begonnen hat – er kann nicht die Zukunft vorhersagen.
- In **starken Aufwärtsmärkten** liegt er **meist hinter** „einfach SPY halten“: Er hält
  Cash, Anleihen und Gold, und er wird bei Rücksetzern ausgestoppt und kauft später teurer
  zurück.
- In Seitwärtsphasen gibt es Fehlsignale (rein, raus, rein …) – das kostet.
- Ein Backtest zeigt die Vergangenheit. Die Zukunft kann anders aussehen.
- Paper-Trading ist idealisiert (perfekte Ausführungen, keine Steuern, keine Gefühle).

## 7. Lokal auf dem eigenen Computer (optional)

```bash
pip install -r requirements.txt matplotlib
cp .env.example .env        # dann .env öffnen und Paper-Schlüssel eintragen
python bot.py --dry-run     # nur Plan anzeigen (geht auch am Wochenende)
python backtest.py          # Backtest ab 2018 mit Alpaca-Daten
python -m unittest discover -s tests -t .   # Tests (ohne Internet)
```

Backtest mit eigenen Daten: `python backtest.py --csv-dir data/` – im Ordner `data/` liegt
pro ETF eine Datei wie `SPY.csv` mit den Spalten `date,open,high,low,close`.

## 8. Einstellungen (`config.json`)

| Einstellung | Standard | Bedeutung |
|---|---|---|
| `watchlist` | 6 ETFs | Welche ETFs gehandelt werden |
| `benchmark` | `SPY` | Womit verglichen wird |
| `sma_fast` / `sma_slow` | 50 / 200 | Länge der Durchschnitte in Handelstagen |
| `max_position_pct` | 20 | Höchstens so viel % des Kontos pro ETF |
| `cash_reserve_pct` | 5 | So viel % bleibt immer Cash |
| `trailing_stop_pct` | 10 | Abstand des Trailing Stops in % |
| `cooldown_days` | 10 | Tage Sperre nach Stop-Auslösung |

Die Datei wird beim Start geprüft (z. B. muss `sma_fast` kleiner als `sma_slow` sein,
keine doppelten ETFs). Ein Fehler bricht den Lauf mit einer verständlichen Meldung ab.

## Dateien

| Datei | Aufgabe |
|---|---|
| `strategy.py` | Die Regeln (ohne Internet) – benutzt von Bot **und** Backtest |
| `alpaca.py` | Kleiner Alpaca-Client (nur `requests`), Paper-Sperre |
| `bot.py` | Ein Lauf: prüfen, planen, verkaufen, kaufen, Stops abgleichen, Tagebuch |
| `backtest.py` | Dieselben Regeln auf historischen Kursen, Vergleich mit SPY |
| `tests/` | Automatische Tests mit Fake-Broker (kein Internet nötig) |
| `.github/workflows/trade.yml` | Täglicher Lauf und Knöpfe auf GitHub |

**Sicherheit:** Der Bot verweigert jede andere Adresse als
`https://paper-api.alpaca.markets`. Ein Live-Konto ist bewusst nicht vorgesehen.
