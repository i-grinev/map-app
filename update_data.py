import requests
import json
import re
import hashlib
import time
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# КОНФИГУРАЦИЯ (из секретов GitHub)
# ============================================================
BITRIX_WEBHOOK = os.environ.get('BITRIX_WEBHOOK')
ENTITY_TYPE_ID = 1038
ADDRESS_FIELD = 'ufCrm8FullAdress'
YANDEX_API_KEY = os.environ.get('YANDEX_API_KEY', '')

MAX_WORKERS = 5
BATCH_SIZE = 20
CACHE_FILE = 'data/geocode_cache.json'
IGNORE_STAGES = ['UC_QA1YNG']

if not BITRIX_WEBHOOK:
    raise Exception("❌ BITRIX_WEBHOOK не задан!")

# ============================================================
# КЭШ (с проверкой изменений адреса)
# ============================================================
def load_cache():
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def get_cache_key(address):
    """Создаёт ключ кэша на основе адреса"""
    return hashlib.md5(address.encode()).hexdigest()

def is_address_changed(item, cache):
    """
    Проверяет, изменился ли адрес с момента последнего кэширования
    """
    address = item.get(ADDRESS_FIELD, '')
    if not address:
        return False
    
    cache_key = get_cache_key(address)
    
    # Если адреса нет в кэше — он изменился (или новый)
    if cache_key not in cache:
        return True
    
    # Проверяем, есть ли у нас сохранённая версия адреса
    cached_data = cache.get(cache_key)
    if not cached_data:
        return True
    
    # Если в кэше есть координаты — адрес не изменился
    if cached_data.get('lat') and cached_data.get('lon'):
        return False
    
    # Если координат нет — адрес нужно перегеокодировать
    return True

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ
# ============================================================
def get_stage_short(stage_id):
    if not stage_id:
        return 'default'
    parts = stage_id.split(':')
    return parts[-1] if parts else 'default'

def should_ignore(stage_id):
    if not stage_id:
        return False
    return get_stage_short(stage_id) in IGNORE_STAGES

# ============================================================
# НОРМАЛИЗАЦИЯ — МИНИМАЛЬНАЯ
# ============================================================
def normalize_address(address):
    """Минимальная очистка: только звёздочки и лишние пробелы"""
    if not address:
        return ''
    
    text = str(address).strip()
    text = text.replace('*', '').replace('#', '')
    text = text.replace('\n', ' ').replace('\r', ' ')
    text = re.sub(r'^Адрес\s*:?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text)
    text = text.strip().rstrip(',').rstrip('.')
    
    return text if len(text) >= 5 else ''

# ============================================================
# ГЕОКОДИРОВАНИЕ
# ============================================================
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
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            members = data.get('response', {}).get('GeoObjectCollection', {}).get('featureMember', [])
            if members:
                pos = members[0]['GeoObject']['Point']['pos']
                lon, lat = pos.split(' ')
                lat, lon = float(lat), float(lon)
                if abs(lat - 55.7558) > 0.01 or abs(lon - 37.6173) > 0.01:
                    return {'lat': lat, 'lon': lon}
        return None
    except:
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
            timeout=15
        )
        if response.status_code == 200 and response.json():
            data = response.json()[0]
            lat, lon = float(data['lat']), float(data['lon'])
            if abs(lat - 55.7558) > 0.01 or abs(lon - 37.6173) > 0.01:
                return {'lat': lat, 'lon': lon}
        return None
    except:
        return None

def geocode_address(address, cache):
    if not address:
        return None
    
    cache_key = get_cache_key(address)
    
    # Проверяем кэш
    if cache_key in cache and cache[cache_key]:
        cached = cache[cache_key]
        # Если есть координаты — возвращаем их
        if cached.get('lat') and cached.get('lon'):
            return cached
    
    # Геокодируем
    coords = None
    
    # 1. Яндекс
    if YANDEX_API_KEY:
        coords = geocode_yandex(address)
        if coords:
            cache[cache_key] = coords
            save_cache(cache)
            return coords
    
    # 2. OSM
    coords = geocode_osm(address)
    if coords:
        cache[cache_key] = coords
        save_cache(cache)
        return coords
    
    # 3. OSM с "Россия"
    if 'Москва' not in address and 'Санкт-Петербург' not in address and 'Краснодар' not in address:
        coords = geocode_osm(address + ', Россия')
        if coords:
            cache[cache_key] = coords
            save_cache(cache)
            return coords
    
    # Не найдено
    cache[cache_key] = None
    save_cache(cache)
    return None

# ============================================================
# ПАРАЛЛЕЛЬНАЯ ОБРАБОТКА
# ============================================================
def process_item(item, cache):
    stage_id = item.get('stageId', '')
    if should_ignore(stage_id):
        return {
            'id': item.get('id'),
            'title': item.get('title', ''),
            'address': item.get(ADDRESS_FIELD, ''),
            'address_clean': '',
            'lat': None,
            'lon': None,
            'stage_id': stage_id,
            'stage_name': item.get('stage_name', ''),
            'ignored': True
        }, False
    
    address = item.get(ADDRESS_FIELD, '')
    clean = normalize_address(address) if address else ''
    
    # Проверяем, нужно ли геокодировать
    coords = None
    if clean:
        # Если адрес в кэше и координаты есть — используем их
        cache_key = get_cache_key(clean)
        if cache_key in cache and cache[cache_key] and cache[cache_key].get('lat'):
            coords = cache[cache_key]
        else:
            # Геокодируем только если адрес изменился или нет в кэше
            coords = geocode_address(clean, cache)
    
    return {
        'id': item.get('id'),
        'title': item.get('title', ''),
        'address': address,
        'address_clean': clean,
        'lat': coords['lat'] if coords else None,
        'lon': coords['lon'] if coords else None,
        'stage_id': stage_id,
        'stage_name': item.get('stage_name', ''),
        'ignored': False
    }, coords is not None

def process_parallel(items, cache):
    results, geocoded, ignored = [], 0, 0
    total, start_time = len(items), time.time()
    print(f"   Запуск {MAX_WORKERS} потоков...")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_item, item, cache): i for i, item in enumerate(items)}
        completed = 0
        for future in as_completed(futures):
            try:
                result, found = future.result(timeout=60)
                results.append(result)
                if found: geocoded += 1
                if result.get('ignored'): ignored += 1
                completed += 1
                if completed % BATCH_SIZE == 0 or completed == total:
                    print(f"   {completed}/{total} | Найдено: {geocoded} | Игнор: {ignored} | {time.time()-start_time:.0f}с")
                time.sleep(0.5)
            except Exception as e:
                print(f"   ❌ Ошибка: {e}")
                completed += 1
    return results, geocoded, ignored

# ============================================================
# ЗАПРОС К БИТРИКС24
# ============================================================
def fetch_from_bitrix():
    all_items, start, limit = [], 0, 50
    print(f"📥 Загрузка из Битрикс24...")
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
            if not items: break
            all_items.extend(items)
            print(f"   Загружено: {len(all_items)}")
            if len(items) < limit: break
            start += limit
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
            break
    return all_items

# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================
def main():
    print(f"🔄 Обновление: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Яндекс ключ: {'✅' if YANDEX_API_KEY else '❌'}")
    
    # Загружаем кэш (НЕ ОЧИЩАЕМ)
    cache = load_cache()
    print(f"   Кэш: {len(cache)} записей")
    
    items = fetch_from_bitrix()
    if not items:
        print("❌ Нет данных")
        return
    
    total_before = len(items)
    items_to_process = [i for i in items if not should_ignore(i.get('stageId', ''))]
    ignored_count = total_before - len(items_to_process)
    print(f"   Всего: {total_before}, игнор: {ignored_count}, обраб: {len(items_to_process)}")
    
    print(f"📍 Геокодирование (используем кэш)...")
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
    
    print(f"\n✅ Готово! Всего: {len(all_results)}, с координатами: {geocoded}, игнор: {ignored_count}")
    print(f"   Файл: {output_file}")

if __name__ == '__main__':
    main()
