#!/bin/bash
# Test script for Asset Allocation Model API

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
API_URL="${API_URL:-http://localhost:8080}"
API_KEY="${API_KEY}"

if [ -z "$API_KEY" ]; then
    echo -e "${RED}Error: API_KEY environment variable is not set${NC}"
    echo "Generate an API key first: python generate_api_key.py"
    exit 1
fi

echo -e "${YELLOW}Testing Asset Allocation Model API${NC}"
echo "API URL: $API_URL"
echo ""

# Test 1: Health check
echo -e "${GREEN}Test 1: Health Check${NC}"
curl -s "$API_URL/health" | jq .
echo ""

# Test 2: Generate without webhook (synchronous)
echo -e "${GREEN}Test 2: Generate Portfolio (Synchronous)${NC}"
curl -s -X POST "$API_URL/asset-allocation/api/v1/generate" \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "storageId": "test_user",
    "fileName": "test_portfolio_v1",
    "riskProfile": "RP1",
    "weightType": "dynamic"
  }' | jq .
echo ""

# Test 3: Generate with webhook (asynchronous)
echo -e "${GREEN}Test 3: Generate Portfolio (Asynchronous with Webhook)${NC}"
curl -s -X POST "$API_URL/asset-allocation/api/v1/generate" \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "storageId": "test_user",
    "fileName": "test_portfolio_v2",
    "riskProfile": "RP1",
    "weightType": "dynamic",
    "webhook": {
      "url": "https://httpbin.org/post",
      "method": "POST",
      "headers": {
        "Authorization": "Bearer test-token"
      }
    }
  }' | jq .
echo ""

# Test 4: Missing required fields (should return 422)
echo -e "${GREEN}Test 4: Missing Required Fields (Expected 422)${NC}"
curl -s -X POST "$API_URL/asset-allocation/api/v1/generate" \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "riskProfile": "RP1"
  }' | jq .
echo ""

# Test 5: Invalid API key (should return 401)
echo -e "${GREEN}Test 5: Invalid API Key (Expected 401)${NC}"
curl -s -X POST "$API_URL/asset-allocation/api/v1/generate" \
  -H "X-Api-Key: invalid-key" \
  -H "Content-Type: application/json" \
  -d '{
    "storageId": "test",
    "fileName": "test"
  }' | jq .
echo ""

echo -e "${GREEN}Tests completed!${NC}"

