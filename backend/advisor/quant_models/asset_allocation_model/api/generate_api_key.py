#!/usr/bin/env python3
"""
Script to generate API keys for Asset Allocation Model API.

Usage:
    python generate_api_key.py

The script reads API_SECRET from environment variables or .env file
and generates an API key that can be used in the X-Api-Key header.
"""
import os
import sys
import hmac
import hashlib
from pathlib import Path
from dotenv import load_dotenv


def generate_api_key(secret: str) -> str:
    """
    Generate an API key from a secret using HMAC.
    
    Args:
        secret: The secret key from environment variable
        
    Returns:
        API key as hexadecimal string
    """
    return hmac.new(
        secret.encode('utf-8'),
        b'api_key',
        hashlib.sha256
    ).hexdigest()


def main():
    """Main entry point for API key generation."""
    load_dotenv(dotenv_path=Path(__file__).parent / '.env')
    api_secret = os.getenv('API_SECRET')
    
    if not api_secret:
        print("Error: API_SECRET environment variable is not set.")
        print("\nPlease set API_SECRET in your .env file or environment:")
        print("  export API_SECRET='your-secret-key'")
        print("\nOr create a .env file with:")
        print("  API_SECRET=your-secret-key")
        sys.exit(1)
    
    api_key = generate_api_key(api_secret)
    
    print("=" * 60)
    print("Asset Allocation Model API Key Generator")
    print("=" * 60)
    print(f"\nGenerated API Key:")
    print(f"  {api_key}")
    print(f"\nUse this key in the X-Api-Key header:")
    print(f"  X-Api-Key: {api_key}")
    print("\n" + "=" * 60)
    print("\n⚠️  Keep this key secure and do not share it publicly!")
    print("=" * 60)


if __name__ == '__main__':
    main()

