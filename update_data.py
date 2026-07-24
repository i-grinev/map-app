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

MAX_WORKERS = 3  # Ещё уменьшаем для избежания 429
BATCH_SIZE = 50
CACHE_FILE = 'data/geocode_cache.json'
LOG_FILE = 'data/geocode_log.txt'

IGNORE_STAGES = ['UC_QA1YNG']

if not BITRIX_WEBHOOK:
    raise Exception("❌ BITRIX_WEBHOOK не задан в переменных окружения!")

# ============================================================
# ИНИЦИАЛИЗАЦИЯ ДИРЕКТОРИЙ
# ============================================================
def init_directories():
    """Создает необходимые директории"""
    os.makedirs('data', exist_ok=True)

# ============================================================
# ЛОГГИРОВАНИЕ (с гарантией записи)
# ============================================================
def log_message(msg, level='INFO'):
    """Запись сообщения в лог-файл с принудительным сбросом буфера"""
    try:
        # Создаем директорию для лога
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = f"[{timestamp}] [{level}] {msg}\n"
        
        # Выводим в консоль
        print(log_entry.strip())
        
        # Записываем в файл с принудительным сбросом
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_entry)
            f.flush()  # Принудительно записываем на диск
            os.fsync(f.fileno())  # Гарантируем запись на диск
            
    except Exception as e:
        print(f"⚠️ Ошибка записи лога: {e}")

def clear_log():
    """Очищает лог-файл при запуске"""
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.write(f"Лог геокодирования {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("="*60 + "\n")
            f.flush()
    except Exception as e:
        print(f"⚠️ Ошибка очистки лога: {e}")

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
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
            f.flush()
    except Exception as e:
        log_message(f"Ошибка сохранения кэша: {e}", 'ERROR')

# ============================================================
# ФУНКЦИЯ ДЛЯ ИЗВЛЕЧЕНИЯ СТАДИИ
# ============================================================
def get_stage_short(stage_id):
    if not stage_id:
        return 'default'
    parts = stage_id.split(':')
    return parts[-1] if parts else 'default'

def should_ignore(stage_id):
    if not stage_id:
        return False
    stage = get_stage_short(stage_id)
    return stage in IGNORE_STAGES

# ============================================================
# НОРМАЛИЗАЦИЯ АДРЕСА (ИСПРАВЛЕННАЯ)
# ============================================================
def normalize_address(address):
    if not address:
        return ''
    
    original = address
    text = str(address).strip()
    
    # Удаляем звездочки и решетки
    text = text.replace('*', '').replace('#', '')
    
    # Заменяем переносы строк на пробелы
    text = text.replace('\n', ' ').replace('\r', ' ')
    
    # Удаляем явные комментарии в скобках (но сохраняем важные ориентиры)
    text = re.sub(r'\([^)]*МЦ[^)]*\)', '', text)
    text = re.sub(r'\([^)]*метро[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\([^)]*ЖК[^)]*\)', '', text, flags=re.IGNORECASE)
    
    # Удаляем лишние части (коды, парковки и т.д.)
    text = re.sub(r',?\s*код домофона[\s\S]*?(?=,|$)', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*домофон[\s\S]*?(?=,|$)', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*ключ[\s\S]*?(?=,|$)', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*парковка[\s\S]*?(?=,|$)', '', text, flags=re.IGNORECASE)
    
    # Преобразуем сокращения (ВАЖНО: в правильном порядке)
    replacements = [
        (r'\bг\.\s*Москва\b', 'Москва'),
        (r'\bг\.\s*Санкт-Петербург\b', 'Санкт-Петербург'),
        (r'\bг\.\b', 'город'),
        (r'\bул\.\b', 'улица'),
        (r'\bпр-д\b', 'проезд'),
        (r'\bпр-кт\b', 'проспект'),
        (r'\bпр-т\b', 'проспект'),
        (r'\bпр\.\b', 'проспект'),
        (r'\bпер\.\b', 'переулок'),
        (r'\bш\.\b', 'шоссе'),
        (r'\bнаб\.\b', 'набережная'),
        (r'\bб-р\b', 'бульвар'),
        (r'\bбул\.\b', 'бульвар'),
        (r'\bпос\.\b', 'поселок'),
        (r'\bд\.\b', 'дом'),
        (r'\bк\.\b', 'корпус'),
        (r'\bстр\.\b', 'строение'),
        (r'\bкорп\.\b', 'корпус'),
    ]
    
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    # Обработка форматов типа "23к7" -> "23 корпус 7"
    text = re.sub(r'(\d+)к(\d+)', r'\1 корпус \2', text, flags=re.IGNORECASE)
    text = re.sub(r'дом\s*(\d+)\s*к(\d+)', r'дом \1 корпус \2', text, flags=re.IGNORECASE)
    
    # Удаляем только ДЕТАЛИ КВАРТИР, но СОХРАНЯЕМ номер дома и корпус
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
        (r',?\s*дверь\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*вход\s*[сС]о стороны[\s\S]*?(?=,|$)', ''),
    ]
    
    for pattern, replacement in patterns_to_remove:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    # Убираем лишние запятые и пробелы
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.strip().rstrip(',').rstrip('.')
    
    # Убираем лишние запятые в конце
    text = re.sub(r',\s*$', '', text)
    
    # Если адрес стал слишком коротким, используем оригинал
    if len(text) < 5:
        return original.replace('*', '').strip()
    
    return text

# ============================================================
# ГЕОКОДИРОВАНИЕ
# ============================================================
def geocode_address(address, cache):
    if not address:
        return None
    
    cache_key = hashlib.md5(address.encode()).hexdigest()
    
    if cache_key in cache and cache[cache_key]:
        log_message(f"Найдено в кэше: {address[:50]}...", 'DEBUG')
        return cache[cache_key]
    
    coords = None
    
    # Пробуем Яндекс (если ключ есть)
    if YANDEX_API_KEY:
        log_message(f"Пробуем Яндекс: {address[:50]}...", 'INFO')
        coords = geocode_yandex(address)
        if coords:
            log_message(f"✅ Яндекс нашел: {coords}", 'SUCCESS')
            cache[cache_key] = coords
            save_cache(cache)
            return coords
        else:
            log_message(f"❌ Яндекс не нашел: {address[:50]}...", 'WARNING')
    
    # Пробуем OSM с задержкой между запросами
    time.sleep(1)  # Увеличиваем задержку
    log_message(f"Пробуем OSM: {address[:50]}...", 'INFO')
    coords = geocode_osm(address)
    if coords:
        log_message(f"✅ OSM нашел: {coords}", 'SUCCESS')
        cache[cache_key] = coords
        save_cache(cache)
        return coords
    else:
        log_message(f"❌ OSM не нашел: {address[:50]}...", 'WARNING')
    
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
        response = requests.get(url, params=params, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            members = data.get('response', {}).get('GeoObjectCollection', {}).get('featureMember', [])
            if members:
                pos = members[0]['GeoObject']['Point']['pos']
                lon, lat = pos.split(' ')
                lat, lon = float(lat), float(lon)
                return {'lat': lat, 'lon': lon}
        else:
            log_message(f"Яндекс статус: {response.status_code}", 'ERROR')
        return None
    except Exception as e:
        log_message(f"Яндекс ошибка: {e}", 'ERROR')
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
        response = requests.get(
            url,
            params=params,
            headers={'User-Agent': 'MapApp/1.0 (https://i-grinev.github.io/map-app)'},
            timeout=5
        )
        
        if response.status_code == 200 and response.json():
            data = response.json()[0]
            lat, lon = float(data['lat']), float(data['lon'])
            return {'lat': lat, 'lon': lon}
        else:
            if response.status_code != 200:
                log_message(f"OSM статус: {response.status_code}", 'WARNING')
        return None
    except Exception as e:
        log_message(f"OSM ошибка: {e}", 'ERROR')
        return None

# ============================================================
# ПАРАЛЛЕЛЬНАЯ ОБРАБОТКА
# ============================================================
def process_item(item, cache):
    stage_id = item.get('stageId', '')
    item_id = item.get('id')
    title = item.get('title', '')
    
    log_message(f"--- Обработка ID: {item_id}, Title: {title} ---", 'INFO')
    
    if should_ignore(stage_id):
        log_message(f"Игнорируем по стадии: {stage_id}", 'INFO')
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
        log_message(f"Адрес пуст", 'WARNING')
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
        log_message(f"После нормализации пусто", 'ERROR')
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
        log_message(f"✅ НАЙДЕНЫ координаты: {coords}", 'SUCCESS')
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
    
    log_message(f"Запуск {MAX_WORKERS} потоков, всего {total} объектов", 'INFO')
    
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
                    msg = f"Прогресс: {completed}/{total} | Найдено: {geocoded} | Игнорировано: {ignored} | Время: {elapsed:.0f}с"
                    log_message(msg, 'INFO')
                    print(f"   {msg}")
                    
            except Exception as e:
                log_message(f"Ошибка в потоке: {e}", 'ERROR')
                completed += 1
    
    return results, geocoded, ignored

# ============================================================
# ЗАПРОС К БИТРИКС24
# ============================================================
def fetch_from_bitrix():
    all_items = []
    start = 0
    limit = 50
    
    log_message("📥 Загрузка из Битрикс24...", 'INFO')
    
    while True:
        params = {
            "entityTypeId": ENTITY_TYPE_ID,
            "select": ['id', 'title', ADDRESS_FIELD, 'stageId', 'stage_name'],
            "order": {"id": "asc"},
            "start": start,
            "limit": limit
        }
        
        try:
            response = requests.post(f"{BITRIX_WEBHOOK}crm.item.list", json=params, timeout=30)
            data = response.json()
            
            if 'error' in data:
                raise Exception(f"Bitrix error: {data.get('error_description')}")
            
            items = data.get('result', {}).get('items', [])
            if not items:
                break
            
            all_items.extend(items)
            log_message(f"Загружено: {len(all_items)} записей", 'INFO')
            
            if len(items) < limit:
                break
            start += limit
            
        except Exception as e:
            log_message(f"Ошибка загрузки: {e}", 'ERROR')
            break
    
    return all_items

# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================
def main():
    # Инициализация директорий
    init_directories()
    
    # Очищаем лог при запуске
    clear_log()
    
    log_message(f"🔄 Обновление данных: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 'INFO')
    log_message(f"   Яндекс ключ: {'✅ Есть' if YANDEX_API_KEY else '❌ Нет'}", 'INFO')
    log_message(f"   Игнорируем стадии: {IGNORE_STAGES}", 'INFO')
    log_message(f"   Потоков: {MAX_WORKERS}", 'INFO')
    
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
    
    # Показываем примеры очищенных адресов
    log_message(f"\n📝 Примеры нормализации:", 'INFO')
    for i, item in enumerate(items_to_process[:3]):
        original = item.get(ADDRESS_FIELD, '')
        clean = normalize_address(original)
        log_message(f"   {i+1}. Оригинал: {original[:60]}...", 'INFO')
        log_message(f"      Очищенный: {clean}", 'INFO')
        print(f"   {i+1}. Оригинал: {original[:60]}...")
        print(f"      Очищенный: {clean}")
    
    log_message(f"\n📍 Начинаем геокодирование...", 'INFO')
    results, geocoded, ignored = process_parallel(items_to_process, cache)
    
    # Добавляем игнорируемые объекты
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
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({
                'updated_at': datetime.now().isoformat(),
                'total': len(all_results),
                'geocoded': geocoded,
                'ignored': ignored_count,
                'items': all_results
            }, f, ensure_ascii=False, indent=2)
            f.flush()
        log_message(f"✅ Файл сохранен: {output_file}", 'SUCCESS')
    except Exception as e:
        log_message(f"❌ Ошибка сохранения файла: {e}", 'ERROR')
    
    total = len(all_results)
    log_message(f"\n✅ Готово! Всего: {total}, с координатами: {geocoded}, игнорировано: {ignored_count}", 'SUCCESS')
    log_message(f"   Файл: {output_file}", 'INFO')
    log_message(f"   Лог: {LOG_FILE}", 'INFO')

if __name__ == '__main__':
    main()
