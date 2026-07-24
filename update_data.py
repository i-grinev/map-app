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
# ПРИНУДИТЕЛЬНЫЕ КООРДИНАТЫ ДЛЯ ИЗВЕСТНЫХ АДРЕСОВ
# ============================================================
FORCED_COORDINATES = {
    # Краснодар
    'Краснодар ГД2к1': {'lat': 45.03547, 'lon': 38.975313},
    'Краснодар ИБ88к3Кв38': {'lat': 45.03547, 'lon': 38.975313},
    'Краснодар ИБ88к3Кв39': {'lat': 45.03547, 'lon': 38.975313},
    'г Краснодар, ул. им. Героя Дангирева, д. 2, корп. 1': {'lat': 45.03547, 'lon': 38.975313},
    'г Краснодар, ул. им. Ивана Беленко, д. 88, корп. 3, кв. 38': {'lat': 45.03547, 'lon': 38.975313},
    'г Краснодар, ул. им. Ивана Беленко, д. 88, корп. 3, кв. 39': {'lat': 45.03547, 'lon': 38.975313},
    
    # ЛП33-2 (Ленинградский проспект 33А)
    'г Москва, проспект Ленинградский, д. 33А, ком. 2': {'lat': 55.788454, 'lon': 37.557583},
    'г Москва, пр-т Ленинградский, д. 33А, ком. 2, этаж 2': {'lat': 55.788454, 'lon': 37.557583},
    'г Москва, проспект Ленинградский, д. 33А, ком. 2, этаж 2': {'lat': 55.788454, 'lon': 37.557583},
    
    # Ялта
    'Ялта, улица Войкова, 39Ак2ЖК "Дарсан"Номер 707': {'lat': 44.497415, 'lon': 34.169506},
    'Ялта, улица Войкова, 39Ак2ЖК "Дарсан"Номер 607': {'lat': 44.497415, 'lon': 34.169506},
    'Ялта, улица Войкова, 39Ак1ЖК "Дарсан"Номер 405': {'lat': 44.497415, 'lon': 34.169506},
    'Ялта, улица Войкова, 39Ак1ЖК "Дарсан"Номер 514': {'lat': 44.497415, 'lon': 34.169506},
    'Ялта, улица Войкова, 39Акорпус 2 Номер 619': {'lat': 44.500391, 'lon': 34.158214},
    'Ялта, улица Войкова, 39Акорпус 2 Номер 609': {'lat': 44.500391, 'lon': 34.158214},
    'Ялта, улица Войкова, 39А, корпус 2 Номер 319': {'lat': 44.500391, 'lon': 34.158214},
    'Ялта, улица Войкова, 39Акорпус 1 Номер 402': {'lat': 44.4998, 'lon': 34.158439},
    'Ялта, улица Войкова, 39Акорпус 1 Номер 209': {'lat': 44.4998, 'lon': 34.158439},
}

def get_forced_coordinates(address, address_clean):
    """
    Проверяет, есть ли принудительные координаты для адреса
    Сначала проверяет по чистому адресу, потом по оригинальному
    """
    # Проверяем по чистому адресу
    if address_clean in FORCED_COORDINATES:
        return FORCED_COORDINATES[address_clean]
    
    # Проверяем по оригинальному адресу
    if address in FORCED_COORDINATES:
        return FORCED_COORDINATES[address]
    
    # Проверяем частичное совпадение
    for key, coords in FORCED_COORDINATES.items():
        if key in address_clean or key in address:
            return coords
    
    return None

# ============================================================
# КЭШ
# ============================================================
def load_cache():
    """Загружает кэш из файла"""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_cache(cache):
    """Сохраняет кэш в файл"""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"   ⚠️ Ошибка сохранения кэша: {e}")

# ============================================================
# НОРМАЛИЗАЦИЯ АДРЕСА
# ============================================================
def normalize_address(address):
    """
    Минимальная очистка адреса
    """
    if not address:
        return ''
    
    text = str(address).strip()
    
    # 1. Убираем звёздочки и решётки
    text = text.replace('*', '').replace('#', '')
    text = text.replace('\n', ' ').replace('\r', ' ')
    
    # 2. Убираем мусор в скобках
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)
    
    # 3. Убираем "Адрес:" в начале
    text = re.sub(r'^Адрес\s*:?\s*', '', text, flags=re.IGNORECASE)
    
    # 4. Заменяем сокращения
    replacements = {
        r'\bул\.\b': 'улица',
        r'\bпр-д\b': 'проезд',
        r'\bпр-кт\b': 'проспект',
        r'\bпр-т\b': 'проспект',
        r'\bпр\.\b': 'проезд',
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
    }
    
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    # 5. Формат "23к7" -> "23 корпус 7"
    text = re.sub(r'(\d+)к(\d+)', r'\1 корпус \2', text, flags=re.IGNORECASE)
    
    # 6. Убираем пояснения в конце
    text = re.sub(r',?\s*код\s*домофона[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*домофон[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*ключ[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*парковка[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*Wi-Fi[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*Важно[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*обязательно[\s\S]*$', '', text, flags=re.IGNORECASE)
    
    # 7. Чистим пробелы
    text = re.sub(r',+', ',', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.strip().rstrip(',').rstrip('.')
    
    if len(text) < 5:
        return text
    
    # 8. Добавляем город, если нет
    cities = ['Москва', 'Санкт-Петербург', 'Краснодар', 'Сочи', 'Ялта', 
              'Казань', 'Екатеринбург', 'Новосибирск', 'Красногорск', 
              'Мытищи', 'Видное', 'Люберцы', 'Химки', 'Долгопрудный']
    
    has_city = any(city in text for city in cities)
    
    if not has_city:
        # Проверяем наличие региона
        regions = ['область', 'край', 'республика', 'район', 'поселок', 'деревня', 'село']
        has_region = any(region in text.lower() for region in regions)
        
        if not has_region:
            text = 'г Москва, ' + text
    
    return text

# ============================================================
# ГЕОКОДИРОВАНИЕ
# ============================================================
def geocode_yandex(address):
    """Геокодирование через Яндекс.Карты"""
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
            return {'lat': lat, 'lon': lon}
        return None
    except:
        return None

def geocode_address_with_cache(item, cache):
    """
    Геокодирует адрес с использованием кэша
    Сначала проверяет принудительные координаты
    Потом кэш
    Потом API
    """
    item_id = str(item.get('id'))
    address = item.get(ADDRESS_FIELD, '')
    
    if not address:
        return None, ''
    
    # Нормализуем адрес
    clean = normalize_address(address)
    if not clean:
        return None, ''
    
    # 1. ПРОВЕРЯЕМ ПРИНУДИТЕЛЬНЫЕ КООРДИНАТЫ
    forced_coords = get_forced_coordinates(address, clean)
    if forced_coords:
        # Сохраняем в кэш
        cache_key = f"item_{item_id}"
        cache[cache_key] = {
            'address': address,
            'address_clean': clean,
            'coords': forced_coords,
            'timestamp': time.time(),
            'forced': True  # Отметка, что это принудительные координаты
        }
        save_cache(cache)
        return forced_coords, clean
    
    # 2. ПРОВЕРЯЕМ КЭШ
    cache_key = f"item_{item_id}"
    if cache_key in cache:
        cached = cache[cache_key]
        # Проверяем, совпадает ли адрес
        if cached.get('address') == address:
            coords = cached.get('coords')
            if coords:
                return coords, cached.get('address_clean', clean)
    
    # 3. ГЕОКОДИРУЕМ (только новые адреса)
    print(f"   🔍 Геокодируем новый адрес: {clean[:50]}...")
    coords = geocode_yandex(clean)
    if not coords:
        coords = geocode_osm(clean)
    
    # Сохраняем в кэш
    cache[cache_key] = {
        'address': address,
        'address_clean': clean,
        'coords': coords,
        'timestamp': time.time(),
        'forced': False
    }
    save_cache(cache)
    
    return coords, clean

# ============================================================
# ПАРАЛЛЕЛЬНАЯ ОБРАБОТКА
# ============================================================
def process_item(item, cache):
    """Обрабатывает один элемент"""
    item_id = item.get('id')
    address = item.get(ADDRESS_FIELD, '')
    stage_id = item.get('stageId', '')
    
    # Геокодируем
    coords, clean = geocode_address_with_cache(item, cache)
    
    return {
        'id': item_id,
        'title': item.get('title', ''),
        'address': address,
        'address_clean': clean,
        'lat': coords['lat'] if coords else None,
        'lon': coords['lon'] if coords else None,
        'stage_id': stage_id,
        'stage_name': item.get('stage_name', ''),
        'cached': cache.get(f"item_{item_id}") is not None,
        'forced': cache.get(f"item_{item_id}", {}).get('forced', False) if cache.get(f"item_{item_id}") else False
    }, coords is not None

def process_parallel(items, cache):
    """Обрабатывает элементы параллельно"""
    results = []
    geocoded = 0
    from_cache = 0
    forced = 0
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
                if result.get('cached'):
                    from_cache += 1
                if result.get('forced'):
                    forced += 1
                
                completed += 1
                if completed % BATCH_SIZE == 0 or completed == total:
                    elapsed = time.time() - start_time
                    print(f"   Обработано: {completed}/{total} | Найдено: {geocoded} | Из кэша: {from_cache} | Принудительно: {forced} | Время: {elapsed:.0f}с")
                    
            except Exception as e:
                print(f"   ❌ Ошибка при обработке элемента: {e}")
                completed += 1
    
    return results, geocoded, from_cache, forced

# ============================================================
# ЗАПРОС К БИТРИКС24
# ============================================================
def fetch_from_bitrix():
    """Загружает все элементы из Битрикс24"""
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
            print(f"   ❌ Ошибка при загрузке: {e}")
            break
    
    return all_items

# ============================================================
# СТАТИСТИКА
# ============================================================
def print_statistics(results, geocoded, from_cache, forced, total):
    """Выводит статистику"""
    print("\n" + "="*60)
    print("📊 СТАТИСТИКА ОБРАБОТКИ")
    print("="*60)
    print(f"   Всего элементов:        {total}")
    print(f"   С координатами:         {geocoded} ({geocoded/total*100:.1f}%)")
    print(f"   Без координат:          {total - geocoded} ({(total-geocoded)/total*100:.1f}%)")
    print(f"   Из кэша:                {from_cache} ({from_cache/total*100:.1f}%)")
    print(f"   Принудительно:          {forced} ({forced/total*100:.1f}%)")
    print(f"   Новых запросов к API:   {total - from_cache} ({(total-from_cache)/total*100:.1f}%)")
    
    # Статистика по городам
    cities = {}
    for r in results:
        addr = r.get('address_clean', '')
        city = 'unknown'
        if 'Краснодар' in addr:
            city = 'Краснодар'
        elif 'Санкт-Петербург' in addr or 'СПб' in addr:
            city = 'Санкт-Петербург'
        elif 'Ялта' in addr:
            city = 'Ялта'
        elif 'Сочи' in addr:
            city = 'Сочи'
        elif 'Москва' in addr:
            city = 'Москва'
        else:
            for c in ['Екатеринбург', 'Новосибирск', 'Казань', 'Красногорск', 'Мытищи', 'Видное', 'Люберцы', 'Химки']:
                if c in addr:
                    city = c
                    break
        
        if city not in cities:
            cities[city] = {'total': 0, 'geocoded': 0, 'forced': 0}
        cities[city]['total'] += 1
        if r.get('lat'):
            cities[city]['geocoded'] += 1
        if r.get('forced'):
            cities[city]['forced'] += 1
    
    print("\n   📍 По городам:")
    for city, data in sorted(cities.items(), key=lambda x: x[1]['total'], reverse=True):
        print(f"      {city}: {data['geocoded']}/{data['total']} ({data['geocoded']/data['total']*100:.1f}%) [принудительно: {data['forced']}]")

# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================
def main():
    print(f"🔄 Обновление данных: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Яндекс ключ: {'✅ Есть' if YANDEX_API_KEY else '❌ Нет'}")
    print(f"   Принудительных координат: {len(FORCED_COORDINATES)}")
    
    # Загружаем кэш
    cache = load_cache()
    cache_size = len(cache)
    print(f"   Кэш: {cache_size} записей")
    
    # Загружаем данные из Битрикс24
    items = fetch_from_bitrix()
    if not items:
        print("❌ Нет данных из Битрикс24")
        return
    
    total = len(items)
    print(f"   Всего элементов для обработки: {total}")
    
    # Обрабатываем
    print(f"\n📍 Геокодирование...")
    results, geocoded, from_cache, forced = process_parallel(items, cache)
    
    # Сохраняем кэш
    save_cache(cache)
    
    # Сохраняем результаты
    output_file = 'data/addresses.json'
    try:
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({
                'updated_at': datetime.now().isoformat(),
                'total': len(results),
                'geocoded': geocoded,
                'from_cache': from_cache,
                'forced': forced,
                'cache_size': len(cache),
                'items': results
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"   ❌ Ошибка сохранения результатов: {e}")
    
    # Выводим статистику
    print_statistics(results, geocoded, from_cache, forced, total)
    
    print(f"\n✅ Готово! Файл: {output_file}")
    print("="*60)

if __name__ == '__main__':
    main()
