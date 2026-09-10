class PiPedalAIError(Exception):
    """Base error for expected application failures."""


class ConfigurationError(PiPedalAIError):
    pass


class CatalogError(PiPedalAIError):
    pass


class ContractError(PiPedalAIError):
    pass


class CompilationError(PiPedalAIError):
    pass


class RemoteServiceError(PiPedalAIError):
    pass


class PiPedalImportError(PiPedalAIError):
    pass

