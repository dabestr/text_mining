import re
import logging
import importlib
from itertools import groupby

import numpy as np
from text_to_num import text2num
from spacy.matcher import Matcher

Sentence = importlib.import_module('Sentence')


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

LONG_ARTICLE = 50  # considered a long article, perhaps signed by a name
LOCATION_NAME_WORD_CUTOFF = 10


class Article:

    def __init__(self,
                 df_row,
                 language,
                 keywords,
                 nlp,
                 locations_df):

        self.text = df_row['text']
        self.title = df_row['title']
        self.doc_with_title = self.title + '.\n' + self.text
        self.article_num = df_row['Unnamed: 0']
        self.publication_date = str(df_row['publish_date'].date())

        self._preprocess_french_number_words()
        self._preprocess_numbers(keywords)
        self._clean(language)
        self._preprocess_titles(keywords['titles'], language)

        # TODO: perhaps use doc_with_title here if article text is below some word count,
        #  but need to be careful of duplicates
        self.doc = nlp(self.text)

        # set location (most) mentioned in the document
        # discard documents with no locations
        self._find_locations(locations_df, nlp)
        self.locations, self.doc_with_title = Sentence.clean_locations(self.locations, self.doc_with_title)
        self._get_doc_location(locations_df)

    def analyze(self, language, keywords, df_impact):
        if self.location is None:
            logger.warning('No locations mentioned in document, not analyzing')
            return
        for s in self.doc.sents:
            sentence = Sentence.Sentence(s, self.doc, self.location_matches, language, self.location)
            final_info_list = sentence.analyze(keywords, language)
            for (location, impact_label, number, addendum) in final_info_list:
                _save_in_dataframe(df_impact, location,
                                   self.publication_date, self.article_num, impact_label,
                                   number, addendum, sentence.sentence_text, self.title)

    def _get_doc_location(self, locations_df):
        if len(self.locations) == 1:
            # easy case, document mentions one location only
            self.location = self.locations[0]
        elif len(self.locations) > 1:
            # multiple locations mentioned, take the most common
            self.location = _most_common(self.locations, locations_df)
        elif len(self.locations) == 0:
            # no location mentioned, document not useful
            self.location = None

    def _find_locations(self, locations_df, nlp):
        """
        Find locations of interest in a given text
        """
        # find locations and append them to list
        matcher = Matcher(nlp.vocab)
        _ = [matcher.add('locations', None, [{'LOWER': location.lower(), 'POS': pos}])
             for location in locations_df['FULL_NAME_RO'] for pos in ['NOUN', 'PROPN']]
        matches = matcher(self.doc)
        # As (longer) articles are often signed, toss out last location if it's close to the end
        # since it is probably someone's name
        if len(self.doc) > LONG_ARTICLE:
            try:
                if matches[-1][1] > len(self.doc) - LOCATION_NAME_WORD_CUTOFF:
                    matches = matches[:-1]
            except IndexError:
                pass
        # Delete any matches that are person entities
        for ent in self.doc.ents:
            if ent.label_ == 'PER':
                matches = [match for match in matches
                           if self.doc[match[1]:match[2]].text not in ent.text]
        self.location_matches = matches
        self.locations = [self.doc[i:j].text for (_, i, j) in self.location_matches]

    def _preprocess_titles(self, titles, language):
        # Remove proper names of people because they can have names of towns
        name_replacement = {
            'english': 'someone',
            'french': "quelq'un"
        }[language]
        name_pattern_query_list = [
            r'\.\s[A-Za-z]+\s[A-Z][a-z]+',
            r'\s[A-Za-z]+\s[A-Z][a-z]+',
            r'\.\s[A-Za-z]+',
            r'\s[A-Za-z]+',
        ]

        # filter names with titles (Mr., Ms. ...)
        # titles are case insensitive
        for title in titles:
            for query in name_pattern_query_list:
                query_string = r'(?i:{title}){query}'.format(title=title, query=query)
                self.text = re.sub(query_string, name_replacement, self.text)

        # filter article signatures
        article_signature_query_list = [
            r'[A-Z]+\s[A-Z]+\,\s[A-Za-z]+',  # e.g. MONICA KAYOMBO, Ndola
            r'[A-Z]+\s[A-Z]+\n[A-Za-z]+',  # e.g. MONICA KAYOMBO \n Ndola
            r'[A-Z]+\s[A-Z]+\n\n[A-Za-z]+',  # e.g. MONICA KAYOMBO \n\n Ndola
        ]
        for query in article_signature_query_list:
            pattern_signatures = re.compile(query)
            self.text = re.sub(pattern_signatures, '', self.text)

        return self.text

    def _clean(self, language):
        if language == 'english':
            return ''.join([i if (ord(i) < 128) and (i != '\'') else '' for i in self.text])
        else:
            return ''.join([i if (i != '\'') else '' for i in self.text])

    def _preprocess_numbers(self, keywords):
        # merge numbers divided by whitespace: 20, 000 --> 20000
        # Also works with repeated groups, without comma, and with appended currency
        # e.g. 5 861 052 772FCFA --> 5861052772FCFA (won't work with accents though)
        numbers_divided = re.findall('\d+(?:\,*\s\d+\w*)+', self.text)
        if numbers_divided is not None:
            for number_divided in numbers_divided:
                if re.search('(20[0-9]{2}|19[0-9]{2})', number_divided) is not None:
                    continue
                else:
                    number_merged = re.sub('\,*\s', '', number_divided)
                    self.text = re.sub(number_divided, number_merged, self.text)

        # split money: US$20m --> US$ 20000000 or US$20 --> US$ 20
        numbers_changed = []
        for currency in keywords['currency_short']:
            currency_regex = re.sub('\$', '\\\$', currency)
            numbers_divided = re.findall(re.compile(currency_regex+'[0-9.]+\s', re.IGNORECASE), self.text)
            for number_divided in numbers_divided:
                try:
                    number_final = currency + ' ' + re.search('[0-9.]+\s', number_divided)[0]
                    self.text = re.sub(re.sub('\$', '\\\$', number_divided), number_final, self.text)
                except:
                    pass
            numbers_divided = re.findall(re.compile(currency_regex+'[0-9.]+[a-z]', re.IGNORECASE), self.text)
            for number_divided in numbers_divided:
                try:
                    number_split_curr = re.sub(currency_regex, currency_regex+' ', number_divided)
                    number_isolated = re.search('[0-9.]+[a-z]', number_split_curr)[0]
                    number_text = re.search('[0-9.]+', number_isolated)[0]
                    appendix = re.search('[a-z]', number_isolated)[0].lower()
                    # try to convert number and appendix into one number
                    try:
                        number = float(number_text)
                        if appendix == 'b':
                            number *= 1E9
                        elif appendix == 'm':
                            number *= 1E6
                        elif appendix == 'k':
                            number *= 1E3
                        else:
                            print('money conversion failed (', self.text, ') !!!')
                    except:
                        pass
                    number_final = re.sub(appendix, '', str(int(number)))
                    number_final = currency + ' ' + number_final
                    self.text = re.sub(re.sub('\$', '\\\$', number_divided), number_final, self.text)
                    numbers_changed.append(number_final)
                except:
                    pass

    def _preprocess_french_number_words(self):
        # Since the French model has no number entities, need to deal with number words by hand
        # for now. Could eventually train our own cardinal entity, but in the medium
        # term this should probably be made a pipeline component, although the text
        # immutability may be an issue
        french_number_words = {'millier': 1E3, 'milliers': 1E3,
                               'million': 1E6, 'millions': 1E6,
                               'milliard': 1E9, 'milliards': 1E9}

        words = self.text.split(' ')
        for i, word in enumerate(words):
            if word in french_number_words.keys():
                prev_word = words[i-1]
                if re.match('^\\d+$', prev_word):
                    number = int(prev_word)
                    need_to_merge = True
                else:
                    try:
                        number = text2num(str(prev_word))
                        need_to_merge = True
                    except ValueError:
                        number = 2  # Multiply 1 million or whatever by 2
                        need_to_merge = False

                number *= french_number_words[word]
                if need_to_merge:
                    search_text = '{}\\s+{}'.format(prev_word, word)
                else:
                    search_text = word
                self.text = re.sub(search_text, str(int(number)), self.text)


def _most_common(lst, locations_df):
    location_counts = [(i, len(list(c))) for i, c in groupby(sorted(lst))]
    # Find the places(s) with max counts
    counts = [count[1] for count in location_counts]
    idx_max = np.where(np.max(counts) == counts)[0]
    # Set location to first entry, this works if there is only one location, or as a fallback
    # if the multiple location handling doesn't work
    location = lst[idx_max[0]]
    # If there is more than one location, take the lowest level admin region.
    if len(idx_max) > 1:
        try:
            lst_max = [lst[idx] for idx in idx_max]
            location_info = locations_df[locations_df['FULL_NAME_RO'].isin(lst_max)]
            location = location_info.groupby('FULL_NAME_RO')['ADM1'].min().idxmin()
        except ValueError:  # in case location_info is empty due to string matching problem reasons
            pass
    return location


def _save_in_dataframe(df_impact, location, date, article_num, label, number_or_text, addendum, sentence, title):
    """
    Save impact data in dataframe, sum entries if necessary
    """
    final_index = (location, date, article_num)
    # first, check if there's already an entry for that location, date and label
    # if so, sum new value to existing value
    if final_index in df_impact.index:
        if str(df_impact.loc[final_index, label]) != 'nan':
            new_value = sum_values(str(df_impact.loc[final_index, label]), number_or_text, addendum, label)
        else:
            new_value = number_or_text
        df_impact.loc[final_index, label] = new_value
        new_sentence = sum_values(df_impact.loc[final_index, 'sentence(s)'],
                                  sentence, '', 'sentence(s)')
        new_title = sum_values(df_impact.loc[final_index, 'article_title'], title, '', 'title')
        df_impact.loc[final_index, ['sentence(s)', 'article_title']] = [new_sentence, new_title]
        return
    # otherwise just save the new entry
    df_impact.loc[final_index, label] = str(number_or_text+' '+addendum).strip()
    df_impact.loc[final_index, ['sentence(s)', 'article_title']] = [sentence, title]


def sum_values(old_string, new_string, new_addendum, which_impact_label):

    final_number = ''
    final_addendum = ''

    if (which_impact_label == 'damage_livelihood') or (which_impact_label == 'damage_general'):
        for (number, currency) in re.findall('([0-9\.]+)[\s]+(.+)', old_string):
            if  new_addendum == currency:
                if int(number) == int(new_string):
                    # same number, probably a repetition... do not sum
                    final_number = str(int(number))
                else:
                    final_number = str(int(number) + int(new_string))
                final_addendum = new_addendum
            else:
                print('different currencies, dont know how to sum !!!!')

    elif (which_impact_label == 'houses_affected') or (which_impact_label == 'people_affected') or (which_impact_label == 'people_dead'):
        final_number = str(int(old_string) + int(new_string))

    else:
        #TODO: figure out why this isn't catching all duplicate sentences
        if (new_string.lower() not in old_string.lower() and
                old_string.lower() not in new_string.lower()):
              final_number = old_string + ', ' + new_string
              final_addendum = new_addendum
        else:
            final_number = old_string

    return str(final_number + ' ' + final_addendum).strip()
