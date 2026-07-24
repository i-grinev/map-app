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

# Настройки скорости
MAX_WORKERS = 10
BATCH_SIZE = 50
CACHE_FILE = 'data/geocode_cache.json'

if not BITRIX_WEBHOOK:
    raise Exception("❌ BITRIX_WEBHOOK не задан в переменных окружения!")

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
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# ============================================================
# НОРМАЛИЗАЦИЯ АДРЕСА (МИНИМАЛЬНАЯ — только убираем мусор)
# ============================================================
def normalize_address(address):
    """
    Минимальная очистка адреса:
    - Убираем звёздочки, решётки
    - Убираем лишние переносы строк
    - Всё остальное ОСТАВЛЯЕМ для геокодера
    """
    if not address:
        return ''
    
    text = str(address).strip()
    
    # Убираем звёздочки, решётки
    text = text.replace('*', '').replace('#', '')
    
    # Заменяем переносы строк на пробелы
    text = text.replace('\n', ' ').replace('\r', ' ')
    
    # Убираем множественные пробелы
    text = re.sub(r'\s+', ' ', text)
    
    # Убираем лишние запятые в конце
    text = text.strip().rstrip(',').rstrip('.')
    
    # Если адрес слишком короткий — возвращаем как есть
    if len(text) < 5:
        return text
    
    return text

# ============================================================
# ГЕОКОДИРОВАНИЕ (с полным адресом)
# ============================================================
def geocode_address(address, cache):
    if not address:
        return None
    
    cache_key = hashlib.md5(address.encode()).hexdigest()
    
    # Проверяем кэш
    if cache_key in cache and cache[cache_key]:
        return cache[cache_key]
    
    # Пробуем геокодеры
    coords = None
    
    # 1. Яндекс (если есть ключ)
    if YANDEX_API_KEY:
        coords = geocode_yandex(address)
        if coords:
            cache[cache_key] = coords
            save_cache(cache)
            return coords
    
    # 2. OpenStreetMap (бесплатно, без ключа)
    coords = geocode_osm(address)
    if coords:
        cache[cache_key] = coords
        save_cache(cache)
        return coords
    
    # Не найдено
    cache[cache_key] = None
    save_cache(cache)
    return None

def geocode_yandex(address):
    """Геокодирование через Яндекс"""
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
                # Проверяем, что это не центр Москвы
                if abs(lat - 55.7558) > 0.01 or abs(lon - 37.6173) > 0.01:
                    return {'lat': lat, 'lon': lon}
        return None
    except:
        return None

def geocode_osm(address):
    """Геокодирование через OpenStreetMap"""
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
            if abs(lat - 55.7558) > 0.01 or abs(lon - 37.6173) > 0.01:
                return {'lat': lat, 'lon': lon}
        return None
    except:
        return None

# ============================================================
# ПАРАЛЛЕЛЬНАЯ ОБРАБОТКА
# ============================================================
def process_item(item, cache):
    """Обработка одного элемента"""
    # Берём ПОЛНЫЙ адрес
    address = item.get(ADDRESS_FIELD, '')
    clean = normalize_address(address) if address else ''
    
    coords = None
    if clean:
        coords = geocode_address(clean, cache)
    
    return {
        'id': item.get('id'),
        'title': item.get('title', ''),
        'address': address,
        'address_clean': clean,
        'lat': coords['lat'] if coords else None,
        'lon': coords['lon'] if coords else None,
        'stage_id': item.get('stageId', ''),
        'stage_name': item.get('stage_name', '')
    }, coords is not None

def process_parallel(items, cache):
    """Параллельная обработка всех адресов"""
    results = []
    geocoded = 0
    total = len(items)
    start_time = time.time()
    
    print(f"   Запуск {MAX_WORKERS} параллельных потоков...")
    
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
                
                completed += 1
                if completed % BATCH_SIZE == 0 or completed == total:
                    elapsed = time.time() - start_time
                    print(f"   Обработано: {completed}/{total} | Найдено: {geocoded} | Время: {elapsed:.0f}с")
                    
            except Exception as e:
                print(f"   ❌ Ошибка: {e}")
                completed += 1
    
    return results, geocoded

# ============================================================
# ЗАПРОС К БИТРИКС24
# ============================================================
def fetch_from_bitrix():
    all_items = []
    start = 0
    limit = 50
    
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
            if not items:
                break
            
            all_items.extend(items)
            print(f"   Загружено: {len(all_items)} записей")
            
            if len(items) < limit:
                break
            start += limit
            
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
            break
    
    return all_items

# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================
def main():
    print(f"🔄 Обновление данных: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Яндекс ключ: {'✅ Есть' if YANDEX_API_KEY else '❌ Нет'}")
    
    # Загружаем кэш
    cache = load_cache()
    print(f"   Кэш: {len(cache)} записей")
    
    # Получаем данные из Битрикса
    items = fetch_from_bitrix()
    if not items:
        print("❌ Нет данных из Битрикс24")
        return
    
    # Параллельное геокодирование
    print(f"📍 Геокодирование ({len(items)} адресов)...")
    results, geocoded = process_parallel(items, cache)
    
    # Сохраняем кэш
    save_cache(cache)
    
    # Сохраняем результаты
    output_file = 'data/addresses.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({
            'updated_at': datetime.now().isoformat(),
            'total': len(results),
            'geocoded': geocoded,
            'items': results
        }, f, ensure_ascii=False, indent=2)
    
    total = len(results)
    print(f"\n✅ Готово! Всего: {total}, с координатами: {geocoded} ({geocoded/total*100:.0f}%)")
    print(f"   Файл: {output_file}")

if __name__ == '__main__':
    main()
