from clients.base import InventoryClient
from config import INSTAMART, instamart_headers, resolve_store_for_pincode


class InstamartClient(InventoryClient):
    def __init__(self):
        super().__init__(INSTAMART)

    async def search_at_store(self, query: str, store: dict, offset: int = 0) -> dict:
        store_id = store["id"]
        return await self.request(
            "/api/instamart/search/v2",
            method="POST",
            params={
                "offset": offset,
                "ageConsent": "false",
                "layoutId": 4987,
                "voiceSearchTrackingId": "",
                "storeId": store_id,
                "primaryStoreId": store_id,
                "secondaryStoreId": "",
            },
            json={
                "facets": [],
                "sortAttribute": "",
                "query": query,
                "search_results_offset": str(offset),
                "page_type": "INSTAMART_AUTO_SUGGEST_PAGE",
                "is_pre_search_tag": False,
            },
            headers=instamart_headers(),
        )

    async def search(self, query: str, pincode: str, offset: int = 0) -> dict:
        store = await resolve_store_for_pincode("instamart", pincode)
        raw = await self.search_at_store(query, store, offset)
        raw["_store"] = store
        return raw

    def parse_inventory(self, raw: dict) -> list[dict]:
        rows: list[dict] = []
        self._walk(raw.get("data", {}).get("cards", []), rows)
        return rows

    def _walk(self, node, rows: list[dict]) -> None:
        if isinstance(node, dict):
            items = node.get("items")
            if isinstance(items, list):
                for entry in items:
                    if isinstance(entry, dict) and "variations" in entry:
                        rows.extend(self._flatten_item(entry))
            for value in node.values():
                self._walk(value, rows)
        elif isinstance(node, list):
            for value in node:
                self._walk(value, rows)

    @staticmethod
    def _flatten_item(item: dict) -> list[dict]:
        rows = []
        for v in item.get("variations") or [{}]:
            price = v.get("price") or {}
            mrp = price.get("mrp") or {}
            offer = price.get("offerPrice") or {}
            inventory = v.get("inventory") or {}
            rows.append({
                "name": v.get("displayName") or item.get("displayName"),
                "brand": item.get("brand"),
                "sku_id": v.get("skuId"),
                "in_stock": inventory.get("inStock", item.get("inStock")),
                "mrp": mrp.get("units"),
                "offer_price": offer.get("units"),
                "quantity_description": v.get("quantityDescription"),
                "category": v.get("category"),
            })
        return rows
