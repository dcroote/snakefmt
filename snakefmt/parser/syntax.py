import tokenize
from snakefmt.types import namedtuple, Token, TokenIterator

from snakefmt.exceptions import (
    DuplicateKeyWordError,
    EmptyContextError,
    NoParametersError,
    InvalidParameterSyntax,
    TooManyParameters,
    InvalidParameter,
    NamedKeywordError,
)

accept_python_code = {"run", "onstart", "onsuccess", "onerror"}
rule_like = {"rule", "checkpoint"}
possibly_named_keywords = rule_like  # Though it may not always be that way


def is_colon(token):
    return token.type == tokenize.OP and token.string == ":"


QUOTES = {'"', "'"}
BRACKETS_OPEN = {"(", "[", "{"}
BRACKETS_CLOSE = {")", "]", "}"}
TAB = "    "


def brack_open(token):
    return token.type == tokenize.OP and token.string in BRACKETS_OPEN


def brack_close(token):
    return token.type == tokenize.OP and token.string in BRACKETS_CLOSE


def is_equal_sign(token):
    return token.type == tokenize.OP and token.string == "="


def is_comma_sign(token):
    return token.type == tokenize.OP and token.string == ","


def not_to_ignore(token):
    return (
        len(token.string) > 0
        and not token.string.isspace()
        and not token.type == tokenize.COMMENT
    )


class Syntax:
    def __init__(
        self, keyword_name: str, target_indent: int, snakefile: TokenIterator = None
    ):
        self.keyword_name = keyword_name
        assert target_indent >= 0
        self.target_indent = target_indent
        self.cur_indent = max(self.target_indent - 1, 0)
        self.comment = ""
        self.token = None

        if snakefile is not None:
            self.parse_and_validate_keyword(snakefile)

    def parse_and_validate_keyword(self, snakefile: TokenIterator):
        self.token = next(snakefile)

        if not is_colon(self.token):
            if self.keyword_name in possibly_named_keywords:
                if self.token.type != tokenize.NAME:
                    raise NamedKeywordError(
                        f"{self.line_nb}Invalid name {self.token.string} for '{self.keyword_name}'"
                    )
                self.keyword_name += f" {self.token.string}"
                self.token = next(snakefile)
        if not is_colon(self.token):
            raise SyntaxError(
                f"{self.line_nb}Colon (not '{self.token.string}') expected after '{self.keyword_name}'"
            )
        self.token = next(snakefile)

        if self.token.type == tokenize.COMMENT:
            self.comment = f" {self.token.string}"
            self.token = next(snakefile)

    @property
    def line_nb(self):
        return f"L{self.token.start[0]}: "


"""
Keyword parsing
"""


class KeywordSyntax(Syntax):
    Status = namedtuple("Status", ["token", "indent", "buffer", "eof"])

    def __init__(
        self,
        keyword_name: str,
        target_indent: int,
        incident_context: Syntax = None,
        snakefile: TokenIterator = None,
        accepts_py: bool = False,
    ):
        super().__init__(keyword_name, target_indent, snakefile)
        self.processed_keywords = set()
        self.accepts_python_code = accepts_py
        self.queriable = True

        if incident_context is not None:
            incident_context.add_processed_keyword(self.token, self.keyword_name)
            if self.token.type != tokenize.NEWLINE:
                raise SyntaxError(
                    f"{self.line_nb}Newline expected after keyword '{self.keyword_name}'"
                )

    def add_processed_keyword(self, token: Token, keyword: str = ""):
        if keyword is "":
            keyword = token.string
        if keyword in self.processed_keywords:
            raise DuplicateKeyWordError(
                f"L{token.start[0]}: '{keyword}' specified twice."
            )
        self.processed_keywords.add(keyword)

    def check_empty(self):
        if len(self.processed_keywords) == 0:
            raise EmptyContextError(
                f"{self.line_nb}{self.keyword_name} has no keywords attached to it."
            )

    def get_next_queriable(self, snakefile):
        buffer = ""
        newline, used_name = False, True
        while True:
            token = next(snakefile)
            t_t = token.type
            if t_t == tokenize.NAME:
                if self.cur_indent <= self.target_indent:
                    if self.queriable:
                        self.queriable = False
                        return self.Status(token, self.cur_indent, buffer, False)
                if used_name:
                    buffer += " "
                else:
                    used_name = True
            else:
                used_name = False
            if t_t == tokenize.INDENT:
                self.cur_indent += 1
                continue
            elif t_t == tokenize.DEDENT:
                if self.cur_indent > 0:
                    self.cur_indent -= 1
                continue
            elif t_t == tokenize.ENDMARKER:
                return self.Status(token, self.cur_indent, buffer, True)
            elif t_t == tokenize.NEWLINE or t_t == tokenize.NL:
                self.queriable, newline = True, True
                buffer += "\n"
                continue
            elif t_t == tokenize.STRING and token.string[0] not in QUOTES:
                buffer += " "
            if newline:
                buffer += TAB * self.cur_indent
                newline = False
            buffer += token.string


"""
Parameter parsing
"""


class Parameter:
    def __init__(self):
        self.key = ""
        self.value = ""
        self.comments = list()
        self.is_string = True
        self.len = 0

    def has_key(self) -> bool:
        return len(self.key) > 0

    def has_value(self) -> bool:
        return len(self.value) > 0

    def add_elem(self, token: Token):
        if token.type != tokenize.STRING:
            self.is_string = False
        if len(self.value) > 0 and token.type == tokenize.NAME:
            self.value += " "

        self.value += token.string

    def to_key_val_mode(self, token: Token):
        if not self.has_value():
            raise InvalidParameterSyntax(
                f"L{token.start[0]}:Operator = used with no preceding key"
            )
        try:
            exec(f"{self.value} = 0")
        except SyntaxError:
            raise InvalidParameterSyntax(
                f"L{token.start[0]}:Invalid key {self.value}"
            ) from None
        self.key = self.value
        self.value = ""
        self.is_string = True


class ParameterSyntax(Syntax):
    def __init__(
        self,
        keyword_name: str,
        target_indent: int,
        incident_vocab,
        snakefile: TokenIterator,
    ):
        super().__init__(keyword_name, target_indent, snakefile)
        self.processed_keywords = set()
        self.positional_params = list()
        self.keyword_params = list()
        self.eof = False
        self.incident_vocab = incident_vocab
        self._brackets = list()

        self.parse_params(snakefile)

    @property
    def all_params(self):
        return self.positional_params + self.keyword_params

    @property
    def in_brackets(self):
        return len(self._brackets) > 0

    def parse_params(self, snakefile: TokenIterator):
        self.found_newline, self.in_lambda = False, False
        cur_param = Parameter()

        while True:
            cur_param = self.process_token(cur_param)
            try:
                self.token = next(snakefile)
            except StopIteration:
                self.flush_param(cur_param, skip_empty=True)
                self.eof = True
                break
            if self.check_exit(cur_param):
                break

        if self.num_params() == 0:
            raise NoParametersError(f"{self.line_nb}In {self.keyword_name} definition.")

    def check_exit(self, cur_param: Parameter):
        if self.found_newline and not_to_ignore(self.token):
            if self.cur_indent < self.target_indent:
                self.flush_param(cur_param, skip_empty=True)
                return True
        return False

    def process_token(self, cur_param: Parameter):
        t_t = self.token.type
        if t_t == tokenize.INDENT:
            self.cur_indent += 1
        elif t_t == tokenize.DEDENT:
            self.cur_indent -= 1
        elif t_t == tokenize.NEWLINE or t_t == tokenize.NL:
            self.found_newline = True
        elif t_t == tokenize.COMMENT:
            cur_param.comments.append(" " + self.token.string)
        elif is_equal_sign(self.token) and not self.in_brackets:
            cur_param.to_key_val_mode(self.token)
        elif is_comma_sign(self.token) and not self.in_brackets and not self.in_lambda:
            self.flush_param(cur_param)
            cur_param = Parameter()
        elif t_t != tokenize.ENDMARKER:
            if brack_open(self.token):
                self._brackets.append(self.token.string)
            if brack_close(self.token):
                self._brackets.pop()
            if is_colon(self.token) and self.in_lambda:
                self.in_lambda = False
            if len(cur_param.value.split()) == 1:
                if cur_param.value == "lambda":
                    self.in_lambda = True
                if self.incident_vocab.recognises(cur_param.value):
                    raise InvalidParameterSyntax(
                        f"{self.line_nb}Over-indented recognised keyword found: '{cur_param.value}'"
                    )
            cur_param.add_elem(self.token)
        return cur_param

    def flush_param(self, parameter: Parameter, skip_empty: bool = False):
        if not parameter.has_value() and skip_empty:
            return

        if parameter.has_key():
            self.keyword_params.append(parameter)
        else:
            self.positional_params.append(parameter)

    def num_params(self):
        return len(self.keyword_params) + len(self.positional_params)


class SingleParam(ParameterSyntax):
    def __init__(
        self,
        keyword_name: str,
        target_indent: int,
        incident_vocab,
        snakefile: TokenIterator = None,
    ):
        super().__init__(keyword_name, target_indent, incident_vocab, snakefile)

        if self.num_params() > 1:
            raise TooManyParameters(
                f"{self.line_nb}{self.keyword_name} definition expects a single parameter"
            )
        if not len(self.keyword_params) == 0:
            raise InvalidParameter(
                f"{self.line_nb}{self.keyword_name} definition requires a positional (not key/value) parameter"
            )


ParamList = ParameterSyntax


class NoKeywordParamList(ParameterSyntax):
    def __init__(
        self,
        keyword_name: str,
        target_indent: int,
        incident_vocab,
        snakefile: TokenIterator = None,
    ):
        super().__init__(keyword_name, target_indent, incident_vocab, snakefile)

        if len(self.keyword_params) > 0:
            raise InvalidParameterSyntax(
                f"{self.line_nb}{self.keyword_name} definition does not accept key/value parameters"
            )
