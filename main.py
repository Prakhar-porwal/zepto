import argparse
import asyncio
import json

from clients.instamart import InstamartClient

PLATFORMS = {
    "instamart": InstamartClient,
}


async def run(platform: str, pincode: str, query: str) -> list[dict]:
    client = PLATFORMS[platform]()
    raw = await client.search(query, pincode=pincode)
    return client.parse_inventory(raw)


def main():
    parser = argparse.ArgumentParser(description="Search quickcommerce inventory by pincode")
    parser.add_argument("platform", choices=PLATFORMS.keys())
    parser.add_argument("pincode", help="Must have a captured profile, e.g. in instamart_profiles.json")
    parser.add_argument("query")
    args = parser.parse_args()

    result = asyncio.run(run(args.platform, args.pincode, args.query))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
