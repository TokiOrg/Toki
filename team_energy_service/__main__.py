import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "team_energy_service.api:app",
        host=os.getenv("TEAM_ENERGY_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", os.getenv("TEAM_ENERGY_PORT", "8010"))),
        reload=False,
    )
