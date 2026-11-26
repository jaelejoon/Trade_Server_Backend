import concurrent.futures as cf
import csv, datetime as dt, io, json, time, urllib.request, zipfile
from tempfile import TemporaryDirectory
from pathlib import Path

API = 'https://fapi.binance.com/fapi/v1/'
ARCHIVE = 'https://data.binance.vision/data/futures/um/'
MINUTE = 60000
def ms(date):
    return int(dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc).timestamp()*1000)

def fetch(url):
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=35) as response:
                return response.read()
        except Exception as error:
            if getattr(error, 'code', None) == 404 or attempt == 5:
                raise
            time.sleep(2**attempt)

def api(start, end):
    return json.loads(fetch(f'{API}klines?symbol=BTCUSDT&interval=1m&startTime={start}&endTime={end-1}&limit=1500'))

def archive(kind, label):
    url = f'{ARCHIVE}{kind}/klines/BTCUSDT/1m/BTCUSDT-1m-{label}.zip'
    with zipfile.ZipFile(io.BytesIO(fetch(url))) as z:
        with z.open(z.namelist()[0]) as f:
            return [r[:6] for r in csv.reader(io.TextIOWrapper(f)) if r and r[0].isdigit()]

def part(job):
    kind, label, start, end = job
    path = Path(cache.name) / f'{kind}-{label}.csv'
    if path.exists():
        return path
    rows = archive(kind, label) if kind != 'api' else [r[:6] for r in api(start,end)]
    with path.with_suffix('.tmp').open('w', newline='') as f:
        csv.writer(f, lineterminator='\n').writerows(rows)
    path.with_suffix('.tmp').replace(path)
    return path

end = 1795550880000
cache = TemporaryDirectory(prefix='btcusdt-')
today = dt.datetime.fromtimestamp(end/1000,dt.timezone.utc).date()
first = 1567965420000
jobs = []
def add_api(start, stop):
    for cursor in range(start,stop,1500*MINUTE):
        jobs.append(('api',str(cursor),cursor,min(stop,cursor+1500*MINUTE)))
add_api(first,ms('2019-12-31'))
jobs.append(('daily','2019-12-31',0,0))
month = dt.date(2020,1,1)
while True:
    following = dt.date(month.year+(month.month==12), month.month%12+1, 1)
    if following > today or (today-following).days < 7:
        break
    jobs.append(('monthly',month.strftime('%Y-%m'),0,0))
    month = following
day = month
while day < today-dt.timedelta(days=2):
    jobs.append(('daily',str(day),0,0))
    day += dt.timedelta(days=1)
add_api(ms(str(day)),end)
print('Collecting', len(jobs), 'parts', flush=True)
with cf.ThreadPoolExecutor(max_workers=6) as pool:
    paths=[]
    for index,path in enumerate(pool.map(part,jobs),1):
        paths.append(path)
        if index%25==0: print(index,flush=True)

output = Path('BTCUSDT_futures_1m.csv')
temporary_output = output.with_suffix('.csv.part')
previous=first-MINUTE
missing=[]
count=0
with temporary_output.open('w',newline='') as f:
    writer=csv.writer(f,lineterminator='\n')
    writer.writerow(['timestamp','open','high','low','close','volume'])
    for path in paths:
        with path.open() as source:
            for row in csv.reader(source):
                timestamp=int(row[0])
                assert first<=timestamp<end and timestamp%MINUTE==0 and timestamp>previous, (path,timestamp,previous)
                if timestamp!=previous+MINUTE:
                    missing.append((previous+MINUTE,timestamp))
                writer.writerow(row)
                count+=1
                previous=timestamp
if previous+MINUTE<end:
    missing.append((previous+MINUTE,end))
if count != 3793090 or missing != [(1567969200000, 1567969260000)]:
    raise SystemExit(f"Unexpected snapshot: {count} candles, missing ranges: {missing}")
temporary_output.replace(output)
print(f"{output}: {count:,} candles", flush=True)

Path("import_btcusdt_1m.sql").write_text("\\set ON_ERROR_STOP on\nBEGIN;\n\nSELECT 1 / count(*)::int AS account_check\nFROM public.users\nWHERE id = :'user_id'::integer;\n\nCREATE TEMP TABLE btcusdt_import (\n    timestamp_ms bigint PRIMARY KEY,\n    open double precision NOT NULL,\n    high double precision NOT NULL,\n    low double precision NOT NULL,\n    close double precision NOT NULL,\n    volume double precision NOT NULL\n) ON COMMIT DROP;\n\n\\copy btcusdt_import FROM '/tmp/BTCUSDT_futures_1m.csv' WITH (FORMAT csv, HEADER true)\n\nDO $$\nBEGIN\n    IF (SELECT count(*) FROM btcusdt_import) <> 3793090\n       OR (SELECT min(timestamp_ms) FROM btcusdt_import) <> 1567965420000\n       OR (SELECT max(timestamp_ms) FROM btcusdt_import) <> 1795550820000 THEN\n        RAISE EXCEPTION 'Unexpected BTCUSDT CSV row count or date range';\n    END IF;\nEND\n$$;\n\nINSERT INTO public.candles_1m (user_id, symbol, open_time, open, high, low, close, volume)\nSELECT :'user_id'::integer, 'BTCUSDT',\n       timestamp '1970-01-01' + (timestamp_ms / 60000) * interval '1 minute',\n       open, high, low, close, volume\nFROM btcusdt_import\nON CONFLICT (user_id, symbol, open_time) DO NOTHING;\n\nSELECT count(*) AS source_candles_present,\n       min(candle.open_time) AS first_btcusdt_utc,\n       max(candle.open_time) AS last_btcusdt_utc,\n       count(*) FILTER (\n           WHERE (candle.open, candle.high, candle.low, candle.close, candle.volume)\n               IS DISTINCT FROM (source.open, source.high, source.low, source.close, source.volume)\n       ) AS existing_rows_with_different_values\nFROM btcusdt_import AS source\nJOIN public.candles_1m AS candle\n  ON candle.user_id = :'user_id'::integer\n AND candle.symbol = 'BTCUSDT'\n AND candle.open_time = timestamp '1970-01-01' + (source.timestamp_ms / 60000) * interval '1 minute';\n\nCOMMIT;\n", encoding="utf-8")
