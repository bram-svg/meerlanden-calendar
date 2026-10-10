import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

import requests
from icalendar import Calendar, Event


COMPANY_CODE = "800bf8d7-6dd1-4490-ba9d-b419d6dc8a45"
API_BASE_URL = "https://wasteprod2api.ximmio.com/api"

POSTCODE = os.environ.get("POSTCODE", "").replace(" ", "").upper()
HOUSE_NUMBER = os.environ.get("HOUSE_NUMBER", "").strip()
HOUSE_LETTER = os.environ.get("HOUSE_LETTER", "").strip()

OUTPUT_FILE = Path("docs/meerlanden.ics")


def api_post(endpoint, payload):
    url = f"{API_BASE_URL}/{endpoint}"

    response = requests.post(
        url,
        json=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "Meerlanden-GitHub-Calendar/1.0",
        },
        timeout=30,
    )

    response.raise_for_status()

    try:
        return response.json()
    except requests.JSONDecodeError as exc:
        raise RuntimeError(
            f"De API gaf geen geldige JSON terug voor {endpoint}: "
            f"{response.text[:300]}"
        ) from exc


def unwrap_result(data):
    """
    Ondersteunt meerdere vormen van de Ximmio-response:
    - een rechtstreekse lijst;
    - {"data": [...]};
    - {"result": [...]};
    - {"dataList": [...]}.
    """
    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    for key in ("dataList", "data", "result", "results", "items"):
        value = data.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            nested = unwrap_result(value)
            if nested:
                return nested

    return []


def get_first_value(item, *keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value

    return None


def get_address():
    payload = {
        "postCode": POSTCODE,
        "houseNumber": HOUSE_NUMBER,
        "companyCode": COMPANY_CODE,
        "houseLetter": HOUSE_LETTER,
    }

    response = api_post("FetchAdress", payload)
    addresses = unwrap_result(response)

    if not addresses:
        print("Volledige API-response van FetchAdress:")
        print(json.dumps(response, indent=2, ensure_ascii=False))

        raise RuntimeError(
            "Geen adres gevonden. Controleer POSTCODE, HOUSE_NUMBER "
            "en HOUSE_LETTER."
        )

    address = addresses[0]

    unique_address_id = get_first_value(
        address,
        "UniqueId",
        "UniqueAddressID",
        "uniqueAddressID",
        "uniqueId",
        "addressID",
        "AddressID",
    )

    community = get_first_value(
        address,
        "Community",
        "community",
        "City",
        "city",
        "Municipality",
        "municipality",
    )

    if not unique_address_id:
        print("Ontvangen adresrecord:")
        print(json.dumps(address, indent=2, ensure_ascii=False))

        raise RuntimeError(
            "Het unieke adres-ID ontbreekt in de API-response."
        )

    if not community:
        print("Ontvangen adresrecord:")
        print(json.dumps(address, indent=2, ensure_ascii=False))

        raise RuntimeError(
            "De gemeente of community ontbreekt in de API-response."
        )

    return str(unique_address_id), str(community), address


def get_collection_dates(unique_address_id, community):
    start_date = date.today() - timedelta(days=7)
    end_date = date.today() + timedelta(days=370)

    payload = {
        "companyCode": COMPANY_CODE,
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "community": community,
        "uniqueAddressID": unique_address_id,
    }

    response = api_post("GetCalendar", payload)
    collections = unwrap_result(response)

    if not collections:
        print("Volledige API-response van GetCalendar:")
        print(json.dumps(response, indent=2, ensure_ascii=False))

        raise RuntimeError(
            "De API gaf geen inzameldata terug voor dit adres."
        )

    return collections


def parse_collection_date(value):
    if not value:
        return None

    if isinstance(value, dict):
        value = (
            value.get("date")
            or value.get("Date")
            or value.get("value")
        )

    if not value:
        return None

    text = str(value).strip()

    # Ondersteuning voor een Microsoft JSON-datum:
    # /Date(1767225600000)/
    if text.startswith("/Date("):
        milliseconds = int(
            text.replace("/Date(", "")
            .replace(")/", "")
            .split("+")[0]
        )
        return datetime.fromtimestamp(
            milliseconds / 1000,
            tz=timezone.utc,
        ).date()

    iso_text = text.replace("Z", "+00:00")

    try:
        return datetime.fromisoformat(iso_text).date()
    except ValueError:
        pass

    for date_format in (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(text[:10], date_format).date()
        except ValueError:
            continue

    return None


def get_waste_type(item):
    waste_type = item.get("wasteType") or item.get("WasteType")

    if isinstance(waste_type, dict):
        return str(
            waste_type.get("title")
            or waste_type.get("Title")
            or waste_type.get("name")
            or waste_type.get("Name")
            or waste_type.get("code")
            or waste_type.get("Code")
            or "Afvalinzameling"
        )

    return str(
        waste_type
        or item.get("title")
        or item.get("Title")
        or item.get("name")
        or item.get("Name")
        or item.get("description")
        or item.get("Description")
        or "Afvalinzameling"
    )


def get_collection_date(item):
    for key in (
        "pickupDates",
        "PickupDates",
        "dates",
        "Dates",
        "date",
        "Date",
        "pickupDate",
        "PickupDate",
        "collectionDate",
        "CollectionDate",
    ):
        value = item.get(key)

        if value:
            return value

    return None


def flatten_collections(collections):
    """
    Ximmio kan één record per datum teruggeven, maar ook één record
    per afvalstroom met meerdere pickupDates.
    """
    flattened = []

    for item in collections:
        raw_dates = get_collection_date(item)

        if isinstance(raw_dates, list):
            for raw_date in raw_dates:
                flattened.append((item, raw_date))
        else:
            flattened.append((item, raw_dates))

    return flattened


def create_ical(collections, address):
    calendar = Calendar()
    calendar.add("prodid", "-//Meerlanden afvalkalender//NL")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", "Afvalkalender Meerlanden")

    seen_events = set()

    for item, raw_date in flatten_collections(collections):
        collection_date = parse_collection_date(raw_date)

        if not collection_date:
            print(
                "Waarschuwing: datum niet herkend:",
                json.dumps(raw_date, ensure_ascii=False),
            )
            continue

        waste_type = get_waste_type(item).strip()
        event_key = (collection_date.isoformat(), waste_type.lower())

        if event_key in seen_events:
            continue

        seen_events.add(event_key)

        event = Event()

        uid_source = (
            f"meerlanden:{POSTCODE}:{HOUSE_NUMBER}:"
            f"{collection_date.isoformat()}:{waste_type}"
        )

        event.add(
            "uid",
            f"{uuid5(NAMESPACE_URL, uid_source)}@meerlanden-calendar",
        )

        wt = waste_type.upper()

        summary = f"🗑️ {waste_type}"
        description = f"{waste_type} wordt opgehaald"

        if "OPK" in wt:
            summary = "🟦 Papier"
            description = "Papier container wordt geleegd"

        elif "GFT" in wt:
            summary = "🟩 GFT"
            description = "GFT container wordt geleegd"

        elif "REST" in wt:
            summary = "⬛ Restafval"
            description = "Rest container wordt geleegd"

        elif "PMD" in wt:
            summary = "🟧 PMD"
            description = "PMD container wordt geleegd"

        elif "TEXTIEL" in wt:
            summary = "👕 Textiel"
            description = "Textiel wordt ingezameld"

        elif "KERST" in wt:
            summary = "🎄 Kerstboom"
            description = "Kerstboom wordt opgehaald"

        event.add("summary", summary)
        event.add("dtstart", collection_date)
        event.add("dtend", collection_date + timedelta(days=1))
        event.add("dtstamp", datetime.now(timezone.utc))
        event.add("description", description)

        calendar.add_component(event)

    if not seen_events:
        raise RuntimeError(
            "Geen geldige kalenderitems gevonden in de API-response."
        )

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_bytes(calendar.to_ical())

    print(f"Adres gevonden: {json.dumps(address, ensure_ascii=False)}")
    print(f"Aantal kalenderitems: {len(seen_events)}")
    print(f"Kalender opgeslagen als: {OUTPUT_FILE}")


def main():
    if not POSTCODE or not HOUSE_NUMBER:
        raise RuntimeError(
            "POSTCODE en HOUSE_NUMBER moeten als environment variables "
            "of GitHub Actions-variabelen zijn ingesteld."
        )

    unique_address_id, community, address = get_address()

    print(f"Unique address ID: {unique_address_id}")
    print(f"Community: {community}")

    collections = get_collection_dates(
        unique_address_id,
        community,
    )

    create_ical(collections, address)


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        print(f"HTTP-fout bij het benaderen van de API: {exc}")

        if exc.response is not None:
            print(f"Statuscode: {exc.response.status_code}")
            print(f"Response: {exc.response.text[:1000]}")

        sys.exit(1)
    except Exception as exc:
        print(f"Fout: {exc}")
        sys.exit(1)
