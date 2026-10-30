from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from .core import Service, ServiceError


def create_app(*, ontology=None, testing=False, ticket_mode='normal', draft_mode='normal'):
    service = Service(ontology,ticket_mode,draft_mode)
    app = FastAPI(title='FabQuality')

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse(status_code=exc.status,content={'detail':exc.message})

    @app.post('/cases')
    async def receive(request: Request):
        async with request.form() as form:
            # Inspect every supplied file before validating any semantic field.
            files = {}
            for key,value in form.multi_items():
                if hasattr(value,'read'):
                    raw = await value.read(1048577)
                    if len(raw)>1048576:
                        raise ServiceError(413,'File exceeds 1MiB')
                    files[key] = raw
            result = service.receive(form.get('event_id'),form.get('run_id'),files.get('reference'),files.get('test'))
            return {key:result[key] for key in ['case_id','state']}

    @app.get('/cases/{cid}')
    async def get(cid: str):
        return service.get(cid)

    @app.post('/cases/{cid}/step')
    async def step(cid: str):
        return service.step(cid)

    @app.post('/cases/{cid}/decision')
    async def decision(cid: str, request: Request):
        service.get(cid)
        try:
            data = await request.json()
        except Exception:
            raise ServiceError(422,'Expected JSON decision')
        if not isinstance(data,dict) or type(data.get('approve')) is not bool:
            raise ServiceError(422,'approve must be boolean')
        return service.decision(cid,data['approve'])

    @app.post('/eval/run')
    async def evaluate():
        from .evaluation import run
        return run()

    if testing:
        @app.get('/_test/mock')
        async def mock():
            return service.mock.snapshot()
    return app
