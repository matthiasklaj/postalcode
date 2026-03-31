from fastapi import FastAPI, Query
from world_address_validator import AddressValidatorRouter

app = FastAPI(title="World Address Validator")
router = AddressValidatorRouter()

@app.get("/")
def root():
    return {"ok": True}

@app.get("/validate")
def validate(
    country: str = Query(..., min_length=2, max_length=2),
    address: str = Query(..., min_length=3),
):
    result = router.validate(country.upper(), address)
    return result.__dict__