from __future__ import annotations

import dataclasses
import enum
import functools
import locale
import os
import os.path
import sys
import re
from typing import (
    AbstractSet,
    Callable,
    ClassVar,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
    overload,
)

from babel.core import Locale  # type: ignore
from dragonmapper.transcriptions import zhuyin_to_pinyin  # type: ignore
from hangul_romanize import Transliter  # type: ignore
from hangul_romanize.rule import academic  # type: ignore
from hanja import translate  # type: ignore
from jinja2.environment import Environment
from jinja2.filters import contextfilter
from jinja2.loaders import FileSystemLoader
from jinja2.utils import select_autoescape
from markupsafe import Markup
from opencc import OpenCC  # type: ignore
from pinyin_jyutping_sentence import pinyin, jyutping  # type: ignore
from pykakasi import kakasi
from romkan import to_roma  # type: ignore
from yaml import load
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader  # type: ignore


class Spacing(enum.Enum):
    space = 'space'
    no_space = 'no_space'
    implicit_space = 'implicit_space'
    implicit_no_space = 'implicit_no_space'

    def __bool__(self):
        cls: Type[Space] = type(self)
        return self is cls.space or self is cls.implicit_space


@dataclasses.dataclass(frozen=True)
class Term:
    term: str
    space: Spacing
    correspond: str

    def romanize(self, locale: Locale) -> Markup:
        return romanize(self.term, locale)

    def normalize(self, locale: Locale) -> str:
        return self.term


kks = kakasi()


@dataclasses.dataclass(frozen=True)
class EasternTerm(Term):
    read: str

    def romanize(self, locale: Locale) -> Markup:
        return romanize(self.read, locale)

    normalizers: ClassVar[Mapping[Locale, OpenCC]] = {
        Locale.parse('ja'): OpenCC('jp2t'),
        Locale.parse('zh_CN'): OpenCC('s2t'),
        # Locale.parse('zh_HK'): OpenCC('hk2t'),
        # Locale.parse('zh_TW'): OpenCC('tw2t'),
    }

    readers: ClassVar[
        Mapping[
            Locale,
            Callable[[str, str], Iterable[Tuple[str, Union[str, Markup]]]]
        ]
    ] = {
        Locale.parse('ja'): lambda t, n: (
            (t[sum(len(x['orig']) for x in r[:i]):][:len(e['orig'])], e['hira'])
            for r in [kks.convert(n)]
            for i, e in enumerate(r)
        ),
        Locale.parse('ko'): lambda t, n: zip(t, translate(n, 'substitution')),
        Locale.parse('zh_CN'): lambda t, n:
            zip(t, pinyin(n, False, True).split()),
        Locale.parse('zh_HK'): lambda t, n:
            zip(t, jyutping(n, True, True).split()),
        Locale.parse('zh_TW'): lambda t, n:
            zip(t, pinyin(n, False, True).split()),
    }

    def normalize(self, locale: Locale) -> str:
        try:
            normalizer = self.normalizers[locale]
        except KeyError:
            return self.term
        else:
            return normalizer.convert(self.term)

    def read_as(self,
                from_: Locale,
                to: Locale,
                table: Table) -> Iterable[Tuple[str, Union[str, Markup]]]:
        if from_ == to:
            return zip(self.term, self.read.split())
        terms_table: Mapping[str, Term] = table.terms_table.get(to, {})
        term_id = self.normalize(from_)
        correspond = terms_table.get(term_id)
        if isinstance(correspond, type(self)):
            return zip(self.term, correspond.read.split())
        reader = self.readers.get(to)
        term = self.normalize(from_)
        if callable(reader):
            return reader(self.term, term)
        return self.read_as(from_, from_, table)



@dataclasses.dataclass(frozen=True)
class WesternTerm(Term):
    loan: str
    locale: Locale

hangul_romanize_transliter = Transliter(academic)

romanizers: Mapping[Locale, Callable[[str], Markup]] = {
    Locale.parse('ja'): lambda t: Markup(to_roma(t.replace(' ', ''))),
    Locale.parse('ko'): lambda t:
        Markup(hangul_romanize_transliter.translit(t.replace(' ', ''))),
    Locale.parse('zh_HK'): lambda t:
        Markup(re.sub(r'(\d) ?', r'<sup>\1</sup>', t)),
    Locale.parse('zh_TW'): lambda t:
        Markup(zhuyin_to_pinyin(t).replace(' ', '')),
}


def romanize(term: str, locale: Locale) -> Markup:
    global romanizers
    try:
        f = romanizers[locale]
    except KeyError:
        return Markup(term.replace(' ', ''))
    return f(term)


class Word(Sequence[Term]):
    def __init__(self, id: str, locale: Locale, terms: Iterable[Term]):
        self.id = id
        self.locale = locale
        self.terms = list(terms)

    def __iter__(self) -> Iterator[Term]:
        return iter(self.terms)

    def __len__(self) -> int:
        return len(self.terms)

    def romanize(self) -> str:
        return Markup('').join(
            Markup('' if term.space is Spacing.no_space else ' ') +
                term.romanize(self.locale)
            for term in self
        ).strip()

    @overload
    def __getitem__(self, index: int) -> Term: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Term]: ...

    def __getitem__(self, i: Union[int, slice]) -> Union[Term, Sequence[Term]]:
        return self.terms[i]

    def __repr__(self) -> str:
        cls = type(self)
        return f'{cls.__qualname__}.{cls.__name__}({self.id!r}, {self.terms!r})'


class Translation(Mapping[Locale, Sequence[Word]]):
    def __init__(self, translation: Iterable[Tuple[Locale, Sequence[Word]]]):
        self.translation: Mapping[Locale, Sequence[Word]] = dict(translation)

    def __iter__(self) -> Iterator[Tuple[Locale, Sequence[Word]]]:
        return iter(self.translation)

    def __len__(self) -> int:
        return len(self.translation)

    def __getitem__(self, key: Locale) -> Sequence[Word]:
        return self.translation[key]

    @functools.cached_property
    def max_words(self) -> int:
        return max(len(ws) for ws in self.translation.values())

    @functools.cached_property
    def cognate_groups(self) -> Mapping[str, Mapping[Locale, Word]]:
        cognate_groups: Dict[str, Dict[Locale, Word]] = {}
        for locale, words in self.translation.items():
            for word in words:
                cognate_groups.setdefault(word.id, {})[locale] = word
        for word_id in list(cognate_groups):
            if len(cognate_groups[word_id]) < 2:
                del cognate_groups[word_id]
        return cognate_groups

    @functools.cached_property
    def correspondences(self) -> Sequence[str]:
        count_map: Dict[str, int] = {}
        for words in self.values():
            for word in words:
                for term in word:
                    count_map[term.correspond] = \
                        count_map.get(term.correspond, 0) + 1
        counts: List[Tuple[str, int]] = list(count_map.items())
        counts.sort(key=lambda pair: pair[1], reverse=True)
        return [k for k, v in counts if v > 1]


class Table(Sequence[Translation]):
    def __init__(self, translations: Iterable[Translation]):
        self.translations = list(translations)

    @functools.cached_property
    def supported_locales(self) -> AbstractSet[Locale]:
        return frozenset(locale for tr in self for locale in tr)

    @functools.cached_property
    def terms_table(self) -> Mapping[Locale, Mapping[str, Term]]:
        table: Dict[Locale, Dict[str, Term]] = {}
        for translation in self:
            for locale, words in translation.items():
                terms: Dict[str, Term] = table.setdefault(locale, {})
                for word in words:
                    for term in word:
                        terms[term.normalize(locale)] = term
        return table

    def __iter__(self) -> Iterator[Translation]:
        return iter(self.translations)

    def __len__(self) -> int:
        return len(self.translations)

    @overload
    def __getitem__(self, index: int) -> Translation: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Translation]: ...

    def __getitem__(self, i: Union[int, slice]) -> Union[
            Translation, Sequence[Translation]]:
        return self.translations[i]

    def __repr__(self) -> str:
        cls = type(self)
        return f'{cls.__qualname__}.{cls.__name__}({self.translations!r})'


spaceless_languages: AbstractSet[str] = {'ja', 'zh'}


def load_table(path: os.PathLike) -> Table:
    with open(path) as f:
        data = load(f, Loader=Loader)
    table: List[Translation] = []
    assert isinstance(data, Sequence)
    for tr_row in data:
        assert isinstance(tr_row, Mapping)
        translation: List[Tuple[Locale, Sequence[Word]]] = []
        for lang, ws in tr_row.items():
            assert isinstance(lang, str)
            locale = Locale.parse(lang.replace('-', '_'))
            implicit_spacing = \
                Spacing.implicit_no_space \
                if locale.language in spaceless_languages \
                else Spacing.implicit_space
            assert isinstance(ws, Mapping)
            words: List[Word] = []
            for wid, term_rows in ws.items():
                assert isinstance(wid, str)
                assert isinstance(term_rows, Sequence)
                terms: List[Term] = []
                for term_row in term_rows:
                    assert isinstance(term_row, Mapping)
                    t = term_row['term']
                    try:
                        spacing = \
                            Spacing.space \
                            if term_row['space'] \
                            else Spacing.no_space
                    except KeyError:
                        spacing = implicit_spacing
                    term: Term
                    if 'loan' in term_row:
                        term = WesternTerm(
                            t,
                            spacing,
                            term_row.get('correspond', term_row['loan']),
                            term_row['loan'],
                            term_row.get('language', Locale.parse('en'))
                        )
                    elif 'read' in term_row:
                        term = EasternTerm(
                            t,
                            spacing,
                            term_row.get('correspond', t),
                            term_row['read']
                        )
                    else:
                        term = Term(t, spacing, term_row.get('correspond', t))
                    terms.append(term)
                word = Word(wid, locale, terms)
                words.append(word)
            translation.append((locale, words))
        table.append(Translation(translation))
    return Table(table)


territory_names: Mapping[Tuple[str, Locale], str] = {
    # "Hong Kong SAR China" is too long to show in a narrow column:
    ('HK', Locale.parse('en')): 'Hong Kong',
    ('HK', Locale.parse('ja')): '香港',
    ('HK', Locale.parse('ko')): '홍콩',
    ('HK', Locale.parse('zh_CN')): '香港',
    ('HK', Locale.parse('zh_HK')): '香港',
    ('HK', Locale.parse('zh_TW')): '香港',
}

def get_territory_name(territory: Union[Locale, str], language: Locale) -> str:
    if isinstance(territory, Locale):
        territory = territory.territory
    return territory_names.get(
        (territory, language),
        language.territories[territory]
    )


template_loader = FileSystemLoader(os.path.dirname(__file__))
template_env = Environment(
    loader=template_loader,
    autoescape=select_autoescape(['html']),
    extensions=['jinja2.ext.do'],
)
template_env.filters.update(
    dictselect=contextfilter(
        lambda ctx, dict, test=None, *args, **kwargs: {
            k: v
            for k, v in dict.items()
            if (ctx.environment.call_test(test, v, *args, **kwargs) if test else v)
        }
    ),
    territory_name=get_territory_name,
    zip=zip,
)
table_template = template_env.get_template('table.html')


def render_table(locale: Locale, table: Table) -> str:
    supported_locale_map: Mapping[str, AbstractSet[Locale]] = {
        locale.language: {
            l for l in table.supported_locales
            if l.language == locale.language
        }
        for locale in table.supported_locales
    }
    locales: Mapping[str, Union[Locale, Mapping[str, Locale]]] = {
        l: next(iter(ls)) if len(ls) == 1 else {
            '_': Locale(l),
            **{
                l.territory: l
                for l in sorted(
                    ls,
                    key=lambda l: (
                        l != locale,
                        l.territory != locale.territory,
                        get_territory_name(l.territory, locale),
                    )
                )
            }
        }
        for l, ls in sorted(
            supported_locale_map.items(),
            key=lambda pair: (
                pair[0] != 'en',
                pair[0] != locale.language,
                Locale(pair[0]).get_display_name(locale),
            )
        )
    }
    return table_template.render(
        locale=locale,
        locales=locales,
        table=table
    )


def main():
    if len(sys.argv) < 3:
        print('error: too few arguments', file=sys.stderr)
        print('usage:', os.path.basename(sys.argv[0]), 'LANG', 'FILE',
              file=sys.stderr)
        raise SystemExit(1)
    locale = Locale.parse(sys.argv[1])
    table = load_table(sys.argv[2])
    print(render_table(locale, table))


if __name__ == '__main__':
    main()
