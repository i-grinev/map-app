import requests
import json
import re
import hashlib
import time
import os
from datetime import datetime

# ============================================================
# КЛЮЧИ БЕРУТСЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ (GitHub Secrets)
# ============================================================
BITRIX_WEBHOOK = os.environ.get('BITRIX_WEBHOOK')
ENTITY_TYPE_ID = 1038
ADDRESS_FIELD = 'ufCrm8FullAdress'
YANDEX_API_KEY = os.environ.get('YANDEX_API_KEY')

# Проверяем, что ключи есть
if not BITRIX_WEBHOOK:
    raise Exception("❌ BITRIX_WEBHOOK не задан в переменных окружения!")
if not YANDEX_API_KEY:
    print("⚠️ YANDEX_API_KEY не задан, используем OpenStreetMap")

# ============================================================
# КЭШ
# ============================================================
CACHE_FILE = 'data/geocode_cache.json'

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
# НОРМАЛИЗАЦИЯ АДРЕСА
# ============================================================
def normalize_address(address):
    if not address:
        return ''
    
    text = str(address).strip()
    text = re.sub(r'[*#]', '', text)
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)
    text = re.sub(r'^г\.\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^Адрес\s*:?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'(ЖК|МЦД|МЦК|Метро)\s*[«"][^»"]*[»"]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'(ЖК|МЦД|МЦК|Метро)\s+[А-Яа-яёЁA-Za-z]+\s*', '', text, flags=re.IGNORECASE)
    
    replacements = {
        'ул.': 'улица ',
        'пр-д': 'проезд ',
        'пр-кт': 'проспект ',
        'пр.': 'проезд ',
        'пер.': 'переулок ',
        'ш.': 'шоссе ',
        'наб.': 'набережная ',
        'б-р': 'бульвар ',
        'бул.': 'бульвар ',
        'пос.': 'поселок ',
        'д.': 'дом ',
        'к.': 'корпус ',
        'стр.': 'строение ',
        'корп.': 'корпус '
    }
    
    for pattern, replacement in replacements.items():
        text = re.sub(r'\b' + pattern + r'\s*', replacement, text, flags=re.IGNORECASE)
    
    text = re.sub(r'(\d+)к(\d+)', r'\1 корпус \2', text, flags=re.IGNORECASE)
    text = re.sub(r',\s*(подъезд|под\.?|п\.)\s*\d+[А-Яа-яёЁ]?', '', text, flags=re.IGNORECASE)
    text = re.sub(r',\s*(этаж|эт\.?|эт)\s*\d+[А-Яа-яёЁ]?', '', text, flags=re.IGNORECASE)
    text = re.sub(r',\s*(кв\.|квартира|апарт\.?|апартаменты|пом\.|помещение|студия|ком\.|комната)\s*[\d/А-Яа-яёЁ]+', '', text, flags=re.IGNORECASE)
    text = re.sub(r',\s*(код\s*домофона|домофон|ключ|дверь|вход|парковка|Wi-Fi)[\s\S]*$', '', text, flags=re.IGNORECASE)
    
    text = re.sub(r',+', ',', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.strip().rstrip(',').rstrip('.')
    
    if len(text) < 5:
        return text
    
    if not re.search(r'(Москва|Санкт-Петербург|Краснодар|область|край|республика|район|поселок|деревня|село|город)', text, re.IGNORECASE):
        text = 'Москва, ' + text
    
    return text

# ============================================================
# ГЕОКОДИРОВАНИЕ
# ============================================================
def geocode_address(address, cache):
    cache_key = hashlib.md5(address.encode()).hexdigest()
    
    if cache_key in cache and cache[cache_key]:
        return cache[cache_key]
    
    # Яндекс.Карты (если есть ключ)
    if YANDEX_API_KEY:
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
                    if abs(lat - 55.7558) > 0.01 or abs(lon - 37.6173) > 0.01:
                        coords = {'lat': lat, 'lon': lon}
                        cache[cache_key] = coords
                        return coords
        except:
            pass
    
    # OpenStreetMap (бесплатно, без ключа)
    try:
        variants = get_address_variants(address)
        for addr in variants:
            url = f"https://nominatim.openstreetmap.org/search?q={requests.utils.quote(addr)}&format=json&limit=1&accept-language=ru"
            response = requests.get(url, headers={'User-Agent': 'MapApp/1.0'}, timeout=5)
            if response.status_code == 200 and response.json():
                data = response.json()[0]
                lat, lon = float(data['lat']), float(data['lon'])
                if abs(lat - 55.7558) > 0.01 or abs(lon - 37.6173) > 0.01:
                    coords = {'lat': lat, 'lon': lon}
                    cache[cache_key] = coords
                    return coords
            time.sleep(1)
    except:
        pass
    
    return None

def get_address_variants(address):
    variants = [address]
    if 'корпус' in address:
        variants.append(re.sub(r'корпус\s*(\d+)', r'к\1', address))
    if 'строение' in address:
        variants.append(re.sub(r'строение\s*(\d+)', r'стр\1', address))
    parts = address.split(',')
    if len(parts) > 1:
        variants.append(parts[0] + ',' + parts[1])
    for city in ['Москва, ', 'Санкт-Петербург, ', 'Краснодар, ']:
        if address.startswith(city):
            variants.append(address[len(city):])
            break
    if 'Россия' not in address:
        variants.append(address + ', Россия')
    return list(dict.fromkeys(variants))

# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================
def main():
    print(f"🔄 Обновление данных: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    cache = load_cache()
    items = fetch_from_bitrix()
    
    if not items:
        print("❌ Нет данных из Битрикс24")
        return
    
    print("📍 Геокодирование...")
    results = []
    geocoded = 0
    
    for i, item in enumerate(items):
        address = item.get(ADDRESS_FIELD, '')
        clean = normalize_address(address) if address else ''
        
        coords = None
        if clean:
            coords = geocode_address(clean, cache)
            if coords:
                geocoded += 1
        
        results.append({
            'id': item.get('id'),
            'title': item.get('title', ''),
            'address': address,
            'address_clean': clean,
            'lat': coords['lat'] if coords else None,
            'lon': coords['lon'] if coords else None,
            'stage_id': item.get('stageId', ''),
            'stage_name': item.get('stage_name', '')
        })
        
        if (i + 1) % 50 == 0:
            print(f"   Обработано: {i+1}/{len(items)}")
    
    output_file = 'data/addresses.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({
            'updated_at': datetime.now().isoformat(),
            'total': len(results),
            'geocoded': geocoded,
            'items': results
        }, f, ensure_ascii=False, indent=2)
    
    save_cache(cache)
    
    print(f"✅ Готово! Всего: {len(results)}, с координатами: {geocoded} ({geocoded/len(results)*100:.0f}%)")

if __name__ == '__main__':
    main()
