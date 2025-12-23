curl -sS -X POST "https://<dein-service>/api/warnings?typeName=dwd:Warnungen_Gemeinden_vereinigt&max=500" \
  -H "Content-Type: application/json" \
  -d '{"aoi":{"type":"Feature","properties":{},"geometry":{"type":"Polygon","coordinates":[[[8.651,49.872],[8.74,49.872],[8.74,49.93],[8.651,49.93],[8.651,49.872]]]}}}' \
  | jq .
