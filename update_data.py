import requests
import json
import re
import hashlib
import time
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
BITRIX_WEBHOOK = os.environ.get('BITRIX_WEBHOOK')
ENTITY_TYPE_ID = 1038
ADDRESS_FIELD = 'ufCrm8FullAdress'
YANDEX_API_KEY = os.environ.get('YANDEX_API_KEY', '')

MAX_WORKERS = 10
BATCH_SIZE = 50
CACHE_FILE = 'data/geocode_cache.json'
LOG_FILE = 'data/geocode_log.txt'

IGNORE_STAGES = ['UC_QA1YNG']

if not BITRIX_WEBHOOK:
    raise Exception("❌ BITRIX_WEBHOOK не задан в переменных окружения!")

# ============================================================
# ЛОГГИРОВАНИЕ
# ============================================================
def log_message(msg, level='INFO'):
    """Запись сообщения в лог-файл"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] [{level}] {msg}\n"
    
    # Выводим в консоль
    print(log_entry.strip())
    
    # Записываем в файл
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(log_entry)

def log_address_processing(item_id, title, original_address, clean_address, coords, stage_id):
    """Детальное логирование обработки адреса"""
    log_message(f"\n{'='*60}")
    log_message(f"ID: {item_id}, Title: {title}")
    log_message(f"Стадия: {stage_id}")
    log_message(f"Оригинальный адрес: {original_address}")
    log_message(f"Очищенный адрес: {clean_address}")
    log_message(f"Координаты: {coords if coords else 'НЕ НАЙДЕНЫ'}")
    log_message(f"{'='*60}")

# ============================================================
# КЭШ
# ============================================================
def load_cache():
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# ============================================================
# ФУНКЦИЯ ДЛЯ ИЗВЛЕЧЕНИЯ СТАДИИ ИЗ ПОЛНОГО ID
# ============================================================
def get_stage_short(stage_id):
    if not stage_id:
        return 'default'
    parts = stage_id.split(':')
    return parts[-1] if parts else 'default'

# ============================================================
# ПРОВЕРКА — НУЖНО ЛИ ИГНОРИРОВАТЬ
# ============================================================
def should_ignore(stage_id):
    if not stage_id:
        return False
    stage = get_stage_short(stage_id)
    return stage in IGNORE_STAGES

# ============================================================
# НОРМАЛИЗАЦИЯ АДРЕСА (с логированием)
# ============================================================
def normalize_address(address):
    if not address:
        log_message("Адрес пустой", 'WARNING')
        return ''
    
    original = address
    text = str(address).strip()
    
    log_message(f"Начало нормализации: {text[:100]}...", 'DEBUG')
    
    # Удаляем звездочки и решетки
    text = text.replace('*', '').replace('#', '')
    
    # Заменяем переносы строк на пробелы
    text = text.replace('\n', ' ').replace('\r', ' ')
    
    # Удаляем лишние комментарии в скобках
    text = re.sub(r'\([^)]*МЦ[^)]*\)', '', text)
    text = re.sub(r'\([^)]*метро[^)]*\)', '', text, flags=re.IGNORECASE)
    
    # Удаляем явные лишние части
    text = re.sub(r',?\s*код домофона[\s\S]*?(?=,|$)', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*домофон[\s\S]*?(?=,|$)', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*ключ[\s\S]*?(?=,|$)', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*парковка[\s\S]*?(?=,|$)', '', text, flags=re.IGNORECASE)
    
    # Преобразуем сокращения
    replacements = {
        r'\bул\.\b': 'улица',
        r'\bпр-д\b': 'проезд',
        r'\bпр-кт\b': 'проспект',
        r'\bпр-т\b': 'проспект',
        r'\bпр\.\b': 'проспект',
        r'\bпер\.\b': 'переулок',
        r'\bш\.\b': 'шоссе',
        r'\bнаб\.\b': 'набережная',
        r'\bб-р\b': 'бульвар',
        r'\bбул\.\b': 'бульвар',
        r'\bпос\.\b': 'поселок',
        r'\bд\.\b': 'дом',
        r'\bк\.\b': 'корпус',
        r'\bстр\.\b': 'строение',
        r'\bкорп\.\b': 'корпус',
        r'\bг\.\b': 'город',
    }
    
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    # Обработка форматов типа "д. 23к7"
    text = re.sub(r'(\d+)к(\d+)', r'\1 корпус \2', text, flags=re.IGNORECASE)
    text = re.sub(r'дом\s*(\d+)\s*к(\d+)', r'дом \1 корпус \2', text, flags=re.IGNORECASE)
    
    # Удаляем детали квартир/этажей/подъездов
    patterns_to_remove = [
        (r',?\s*кв\.\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*квартира\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*апарт\.?\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*апартаменты\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*пом\.\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*помещение\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*студия\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*ком\.\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*комната\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*этаж\s*\d+[А-Яа-яёЁ]?', ''),
        (r',?\s*эт\.?\s*\d+[А-Яа-яёЁ]?', ''),
        (r',?\s*эт\s*\d+[А-Яа-яёЁ]?', ''),
        (r',?\s*подъезд\s*\d+[А-Яа-яёЁ]?', ''),
        (r',?\s*под\.?\s*\d+[А-Яа-яёЁ]?', ''),
        (r',?\s*п\.\s*\d+[А-Яа-яёЁ]?', ''),
        (r',?\s*секция\s*\d+', ''),
        (r',?\s*парадная\s*\d+', ''),
        (r',?\s*на первом уровне секции', ''),
        (r',?\s*на втором уровне секции', ''),
    ]
    
    for pattern, replacement in patterns_to_remove:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    # Убираем лишние запятые и пробелы
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.strip().rstrip(',').rstrip('.')
    
    # Если адрес стал слишком коротким, используем оригинал
    if len(text) < 5:
        log_message(f"Адрес слишком короткий после очистки: '{text}', используем оригинал", 'WARNING')
        text = original.replace('*', '').strip()
    
    log_message(f"Результат нормализации: {text[:100]}...", 'DEBUG')
    return text

# ============================================================
# ГЕОКОДИРОВАНИЕ (с логированием)
# ============================================================
def geocode_address(address, cache):
    if not address:
        log_message("Попытка геокодирования пустого адреса", 'WARNING')
        return None
    
    cache_key = hashlib.md5(address.encode()).hexdigest()
    log_message(f"Кэш-ключ: {cache_key[:8]}... для адреса: {address[:50]}...", 'DEBUG')
    
    # Проверяем кэш
    if cache_key in cache:
        cached = cache[cache_key]
        if cached:
            log_message(f"Найден в кэше: {cached}", 'INFO')
            return cached
        else:
            log_message(f"В кэше есть запись, но координаты не найдены", 'INFO')
            return None
    
    coords = None
    
    # Пробуем Яндекс
    if YANDEX_API_KEY:
        log_message(f"Пробуем Яндекс геокодинг: {address}", 'INFO')
        coords = geocode_yandex(address)
        if coords:
            log_message(f"Яндекс вернул координаты: {coords}", 'SUCCESS')
            cache[cache_key] = coords
            save_cache(cache)
            return coords
        else:
            log_message(f"Яндекс не нашел координаты", 'WARNING')
    
    # Пробуем OSM
    log_message(f"Пробуем OSM геокодинг: {address}", 'INFO')
    coords = geocode_osm(address)
    if coords:
        log_message(f"OSM вернул координаты: {coords}", 'SUCCESS')
        cache[cache_key] = coords
        save_cache(cache)
        return coords
    else:
        log_message(f"OSM не нашел координаты", 'WARNING')
    
    # Пробуем упрощенный адрес
    simplified = simplify_address(address)
    if simplified and simplified != address:
        log_message(f"Пробуем упрощенный адрес: {simplified}", 'INFO')
        coords = geocode_yandex(simplified) if YANDEX_API_KEY else geocode_osm(simplified)
        if coords:
            log_message(f"Найдены координаты по упрощенному адресу: {coords}", 'SUCCESS')
            cache[cache_key] = coords
            save_cache(cache)
            return coords
    
    log_message(f"Координаты не найдены для адреса: {address}", 'ERROR')
    cache[cache_key] = None
    save_cache(cache)
    return None

def simplify_address(address):
    """Упрощает адрес для поиска (убирает корпуса если не найдено)"""
    simplified = re.sub(r',?\s*корпус\s*\d+', '', address, flags=re.IGNORECASE)
    simplified = re.sub(r',?\s*строение\s*\d+', '', simplified, flags=re.IGNORECASE)
    simplified = re.sub(r',?\s*дом\s*(\d+)[А-Яа-я]?', r'дом \1', simplified, flags=re.IGNORECASE)
    return simplified.strip()

def geocode_yandex(address):
    if not YANDEX_API_KEY:
        log_message("Яндекс API ключ не задан", 'WARNING')
        return None
    
    try:
        url = "https://geocode-maps.yandex.ru/1.x/"
        params = {
            'apikey': YANDEX_API_KEY,
            'geocode': address,
            'format': 'json',
            'results': 1,
            'lang': 'ru_RU'
        }
        
        log_message(f"Запрос к Яндекс: {url}?geocode={address[:50]}...", 'DEBUG')
        
        response = requests.get(url, params=params, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            members = data.get('response', {}).get('GeoObjectCollection', {}).get('featureMember', [])
            
            if members:
                pos = members[0]['GeoObject']['Point']['pos']
                lon, lat = pos.split(' ')
                lat, lon = float(lat), float(lon)
                log_message(f"Яндекс ответ: lat={lat}, lon={lon}", 'DEBUG')
                return {'lat': lat, 'lon': lon}
            else:
                log_message(f"Яндекс: нет результатов", 'WARNING')
        else:
            log_message(f"Яндекс статус: {response.status_code}", 'ERROR')
        
        return None
    except Exception as e:
        log_message(f"Ошибка Яндекс геокодинга: {e}", 'ERROR')
        return None

def geocode_osm(address):
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': address,
            'format': 'json',
            'limit': 1,
            'accept-language': 'ru'
        }
        
        log_message(f"Запрос к OSM: {url}?q={address[:50]}...", 'DEBUG')
        
        response = requests.get(
            url,
            params=params,
            headers={'User-Agent': 'MapApp/1.0 (https://i-grinev.github.io/map-app)'},
            timeout=5
        )
        
        if response.status_code == 200 and response.json():
            data = response.json()[0]
            lat, lon = float(data['lat']), float(data['lon'])
            log_message(f"OSM ответ: lat={lat}, lon={lon}", 'DEBUG')
            return {'lat': lat, 'lon': lon}
        else:
            log_message(f"OSM статус: {response.status_code}, результат: {response.text[:100] if response.text else 'пусто'}", 'WARNING')
        
        return None
    except Exception as e:
        log_message(f"Ошибка OSM геокодинга: {e}", 'ERROR')
        return None

# ============================================================
# ПАРАЛЛЕЛЬНАЯ ОБРАБОТКА
# ============================================================
def process_item(item, cache):
    stage_id = item.get('stageId', '')
    item_id = item.get('id')
    title = item.get('title', '')
    
    log_message(f"\n--- Обработка объекта ID: {item_id}, Title: {title} ---", 'INFO')
    
    if should_ignore(stage_id):
        log_message(f"Объект игнорируется по стадии: {stage_id}", 'INFO')
        return {
            'id': item_id,
            'title': title,
            'address': item.get(ADDRESS_FIELD, ''),
            'address_clean': '',
            'lat': None,
            'lon': None,
            'stage_id': stage_id,
            'stage_name': item.get('stage_name', ''),
            'ignored': True
        }, False
    
    address = item.get(ADDRESS_FIELD, '')
    if not address:
        log_message(f"Адрес отсутствует", 'WARNING')
        return {
            'id': item_id,
            'title': title,
            'address': '',
            'address_clean': '',
            'lat': None,
            'lon': None,
            'stage_id': stage_id,
            'stage_name': item.get('stage_name', ''),
            'ignored': False
        }, False
    
    clean = normalize_address(address)
    log_message(f"Очищенный адрес: {clean}", 'INFO')
    
    if not clean:
        log_message(f"После нормализации адрес пуст", 'ERROR')
        return {
            'id': item_id,
            'title': title,
            'address': address,
            'address_clean': '',
            'lat': None,
            'lon': None,
            'stage_id': stage_id,
            'stage_name': item.get('stage_name', ''),
            'ignored': False
        }, False
    
    coords = geocode_address(clean, cache)
    
    if coords:
        log_message(f"✅ Найдены координаты: {coords}", 'SUCCESS')
    else:
        log_message(f"❌ Координаты НЕ НАЙДЕНЫ", 'ERROR')
    
    return {
        'id': item_id,
        'title': title,
        'address': address,
        'address_clean': clean,
        'lat': coords['lat'] if coords else None,
        'lon': coords['lon'] if coords else None,
        'stage_id': stage_id,
        'stage_name': item.get('stage_name', ''),
        'ignored': False
    }, coords is not None

def process_parallel(items, cache):
    results = []
    geocoded = 0
    ignored = 0
    total = len(items)
    start_time = time.time()
    
    log_message(f"\n{'='*60}")
    log_message(f"Запуск параллельной обработки {total} объектов")
    log_message(f"Потоков: {MAX_WORKERS}")
    log_message(f"Игнорируем стадию: {IGNORE_STAGES}")
    log_message(f"{'='*60}\n")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_item, item, cache): i 
            for i, item in enumerate(items)
        }
        
        completed = 0
        for future in as_completed(futures):
            try:
                result, found = future.result(timeout=30)
                results.append(result)
                if found:
                    geocoded += 1
                if result.get('ignored'):
                    ignored += 1
                
                completed += 1
                if completed % BATCH_SIZE == 0 or completed == total:
                    elapsed = time.time() - start_time
                    log_message(f"Прогресс: {completed}/{total} | Найдено: {geocoded} | Игнорировано: {ignored} | Время: {elapsed:.0f}с", 'INFO')
                    
            except Exception as e:
                log_message(f"❌ Ошибка в потоке: {e}", 'ERROR')
                completed += 1
    
    return results, geocoded, ignored

# ============================================================
# ЗАПРОС К БИТРИКС24
# ============================================================
def fetch_from_bitrix():
    all_items = []
    start = 0
    limit = 50
    
    log_message(f"📥 Загрузка из Битрикс24...", 'INFO')
    
    while True:
        params = {
            "entityTypeId": ENTITY_TYPE_ID,
            "select": ['id', 'title', ADDRESS_FIELD, 'stageId', 'stage_name'],
            "order": {"id": "asc"},
            "start": start,
            "limit": limit
        }
        
        try:
            log_message(f"Запрос start={start}, limit={limit}", 'DEBUG')
            response = requests.post(f"{BITRIX_WEBHOOK}crm.item.list", json=params, timeout=30)
            data = response.json()
            
            if 'error' in data:
                log_message(f"Ошибка Bitrix: {data.get('error_description')}", 'ERROR')
                break
            
            items = data.get('result', {}).get('items', [])
            if not items:
                log_message(f"Больше нет записей", 'INFO')
                break
            
            all_items.extend(items)
            log_message(f"Загружено: {len(all_items)} записей", 'INFO')
            
            if len(items) < limit:
                break
            start += limit
            
        except Exception as e:
            log_message(f"❌ Ошибка загрузки: {e}", 'ERROR')
            break
    
    return all_items

# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================
def main():
    # Очищаем лог-файл при запуске
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        f.write(f"Лог геокодирования {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("="*60 + "\n")
    
    log_message(f"🔄 Обновление данных: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 'INFO')
    log_message(f"   Яндекс ключ: {'✅ Есть' if YANDEX_API_KEY else '❌ Нет'}", 'INFO')
    log_message(f"   Игнорируем стадии: {IGNORE_STAGES}", 'INFO')
    
    cache = load_cache()
    log_message(f"   Кэш: {len(cache)} записей", 'INFO')
    
    items = fetch_from_bitrix()
    if not items:
        log_message("❌ Нет данных из Битрикс24", 'ERROR')
        return
    
    total_before = len(items)
    items_to_process = [item for item in items if not should_ignore(item.get('stageId', ''))]
    ignored_count = total_before - len(items_to_process)
    log_message(f"   Всего: {total_before}, игнорируем: {ignored_count}, обрабатываем: {len(items_to_process)}", 'INFO')
    
    # Выводим первые 5 адресов для проверки
    log_message(f"\nПримеры адресов для проверки:", 'INFO')
    for i, item in enumerate(items_to_process[:5]):
        log_message(f"  {i+1}. ID={item.get('id')}: {item.get(ADDRESS_FIELD, '')[:100]}...", 'INFO')
    
    log_message(f"\n📍 Начинаем геокодирование...", 'INFO')
    results, geocoded, ignored = process_parallel(items_to_process, cache)
    
    ignored_results = []
    for item in items:
        if should_ignore(item.get('stageId', '')):
            ignored_results.append({
                'id': item.get('id'),
                'title': item.get('title', ''),
                'address': item.get(ADDRESS_FIELD, ''),
                'address_clean': '',
                'lat': None,
                'lon': None,
                'stage_id': item.get('stageId', ''),
                'stage_name': item.get('stage_name', ''),
                'ignored': True
            })
    
    all_results = results + ignored_results
    
    save_cache(cache)
    
    output_file = 'data/addresses.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({
            'updated_at': datetime.now().isoformat(),
            'total': len(all_results),
            'geocoded': geocoded,
            'ignored': ignored_count,
            'items': all_results
        }, f, ensure_ascii=False, indent=2)
    
    total = len(all_results)
    log_message(f"\n✅ Готово! Всего: {total}, с координатами: {geocoded}, игнорировано: {ignored_count}", 'SUCCESS')
    log_message(f"   Файл: {output_file}", 'INFO')
    log_message(f"   Лог: {LOG_FILE}", 'INFO')

if __name__ == '__main__':
    main()
