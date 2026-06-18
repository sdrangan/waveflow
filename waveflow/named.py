from dataclasses import dataclass, field
from typing import ClassVar
import re

@dataclass
class NamedObject:
    """
    Base class for objects that have a type name and instance name. 
    """
    type_name: ClassVar[str | None] = None
    """ The type name to use for this object in visualizations. If None, the snake case of class name is used. """

    name : str | None = None
    """ The name of this instance. If None, a default name is generated based on the type name and instance count """

    _instance_count: ClassVar[int]
    """ The number of instances of this class that have been created, used for generating default names """


    @staticmethod
    def _camel_to_snake(name: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._instance_count = 0
        declared_type_name = cls.__dict__.get("type_name")
        if declared_type_name is None:
            cls.type_name = NamedObject._camel_to_snake(cls.__name__)

    def __post_init__(self):
        cls = type(self)
        if self.name is None:
            self.name = f"{cls.type_name}{cls._instance_count}"
            cls._instance_count += 1
 