import asyncio
import functools
import itertools
import math
import random
import discord
import youtube_dl
import datetime as dt 

from aiohttp import request
from discord.ext import commands, tasks
from async_timeout import timeout
from discord.ext.commands import Cog 

# Silence useless bug reports messages
youtube_dl.utils.bug_reports_message = lambda: ''


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** şu kişi tarafından **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError('Aradığınız `{}` ile eşleşen bir şey bulamadım '.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError('Aradığınız `{}` ile eşleşen bir şey bulamadım'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Aradığınız `{}` ile eşleşen bir şey bulamadım'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError('Aradığınız `{}` ile eşleşen bir şey bulamadım'.format(webpage_url))

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} gün'.format(days))
        if hours > 0:
            duration.append('{} saat'.format(hours))
        if minutes > 0:
            duration.append('{} dakika'.format(minutes))
        if seconds > 0:
            duration.append('{} saniye'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='Şimdi oynatılıyor',
                               description='```css\n{0.source.title}\n```'.format(self),
                               color=discord.Color.blurple())
                 .add_field(name='Süre', value=self.source.duration)
                 .add_field(name='Oynatmamı isteyen kişi', value=self.requester.mention)
                 .add_field(name='Yükleyen kişi', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
                 .add_field(name='URL', value='[Tıkla anam]({0.source.url})'.format(self))
                 .set_thumbnail(url=self.source.thumbnail))

        return embed


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                # Try to get the next song within 3 minutes.
                # If no song will be added to the queue in time,
                # the player will disconnect due to performance
                # reasons.
                try:
                    async with timeout(180):  # 3 minutes
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage('DMde çalışmaz bu')

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send('Mierda! bi şey oldu: {}'.format(str(error)))

    @commands.command(name='join', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):
        """VC kanalına katılır"""

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='summon')
    @commands.has_permissions(manage_guild=True)
    async def _summon(self, ctx: commands.Context, *, channel: discord.VoiceChannel = None):
        """join ile tamamen aynı :D

        
        """

        if not channel and not ctx.author.voice:
            raise VoiceError('Önce sen bi gir kanala')

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='leave', aliases=['disconnect'])
    @commands.has_permissions(manage_guild=True)
    async def _leave(self, ctx: commands.Context):
        """Sırayı temizleyip kanaldan çıkar"""

        if not ctx.voice_state.voice:
            return await ctx.send('Hiçbi kanalda değilim zaten smh')

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    @commands.command(name='volume')
    async def _volume(self, ctx: commands.Context, *, volume: int):
        """Sesi ayarlar"""

        if not ctx.voice_state.is_playing:
            return await ctx.send('Bi şey oynamıyo şu an')

        if 0 > volume > 100:
            return await ctx.send('100 ile 0 arası bi şi olması lazım')

        ctx.voice_state.volume = volume / 100
        await ctx.send('Tamamdır bu değere ayarladım {}%'.format(volume))

    @commands.command(name='now', aliases=['current', 'playing'])
    async def _now(self, ctx: commands.Context):
        """Şu anda oynatılan şarkıyı gösterir"""

        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.command(name='pause')
    @commands.has_permissions(manage_guild=True)
    async def _pause(self, ctx: commands.Context):
        """Şarkıyı pause eder"""

        if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='resume')
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context):
        """Pause edilen şarkıyı devam ettirir"""

        if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='stop')
    @commands.has_permissions(manage_guild=True)
    async def _stop(self, ctx: commands.Context):
        """Çalmayı durdurur ve sırayı temizler"""

        ctx.voice_state.songs.clear()

        if not ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction('⏹')

    @commands.command(name='skip')
    async def _skip(self, ctx: commands.Context):
        """Sıradaki şarkıyı geçer. Şarkıyı isteyen kişi direkt olarak geçebilir. Diğer türlü oylama başlatır.
        Kanaldaki kişi sayısı kaç olursa olsun min 3 kişi gerekir
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Bir şey çalmıyor şu anda')

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id == (666466785771520020):
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.message.add_reaction('⏭')
                ctx.voice_state.skip()
            else:
                await ctx.send('Skip oylaması başladı **{}/3**'.format(total_votes))

        else:
            await ctx.send('Sen zaten oy verdin')

    @commands.command(name='queue')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        """Sırayı gösterir

        
        """

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Boş queue.')

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(description='**{} Sıradaki şarkılar:**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Gösterilen sayfa {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: commands.Context):
        """Sıradaki şarkıları karıştırır"""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Boş queue.')

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction('✅')

    @commands.command(name='remove')
    async def _remove(self, ctx: commands.Context, index: int):
        """Sıradan bir şarkıyı kaldırır"""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Boş queue.')

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('✅')

    @commands.command(name='loop')
    async def _loop(self, ctx: commands.Context):
        """Çalan şarkıyı loop'a sokar
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Hiçbir şey oynamıyo şu an')

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.message.add_reaction('✅')

    @commands.command(name='play')
    async def _play(self, ctx: commands.Context, *, search: str):
        """Bir şarkı çalar
        """

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except YTDLError as e:
                await ctx.send('Bir hata oluştu: {}'.format(str(e)))
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                await ctx.send('Sıraya alındı {}'.format(str(source)))

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError('What a joke! Hiçbir kanalda değilsin!')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError('Zaten VCdeyim.')


bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"),
                   description='ha pu bottur')
bot.add_cog(Music(bot))




@bot.event
async def on_ready():
    print('Logged in as:\n{0.user.name}\n{0.user.id}'.format(bot))
    await bot.change_presence(activity=discord.Game('Komut listesini görmek için !help yazın'))

@bot.event
async def on_message(message):
    if 'hamilton' in message.content:
        myEmbed = discord.Embed(title = "Eğer hâlâ izlemediyseniz bu videoyu izleyin 🤣", color = 0x00ff00)
        myEmbed.add_field(name = 'Link burada <:kral:789265041664245790>: ', value = 'https://www.youtube.com/watch?v=kq2E7LBClnY')
        await message.channel.send(embed = myEmbed)
    await bot.process_commands(message)

    if message.content == "iyi geceler":
        
        await message.channel.send('İyi geceler, tatlı rüyalar! <:kral:789265041664245790><:kral:789265041664245790> ')

    if 'herkes' in message.content:
        await message.channel.send('*herkez')
    elif 'boşver' in message.content:
        await message.channel.send(file = discord.File('space.png'))
        await message.channel.send("*boş ver")
    elif 'erdoğan' in message.content:
        await message.channel.send(file = discord.File('erdogan.jpg'))
        await message.channel.send('He do be watchin')
    
    if  message.content == "öpücük":
        if message.author.id == (666466785771520020):
            await message.channel.send('<:nah:786744888267374642> sana öpücük')
        else:
            await message.channel.send(file = discord.File('resim3.jpg'))

    if message.content.startswith("echo"):
        if message.author.id == (666466785771520020):
            await message.channel.send(message.content[5:].format(message))
            await message.delete()
        else:
            await message.channel.send('Nice try <:nah:786744888267374642>')



@bot.command(name = 'bm')
async def bm(context):
    
    bm_quotes = [
        "**Biliyor muydunuz?**: F1 takvimindeki en kısa pist Monako. Bu uzunluğa erişmek için 642 Infiniti Red Bull Racing RB9’u yan yana dizmeniz gerekiyor.",
        "**Biliyor muydunuz?**: Bahreyn’deki pist yüzeyinin tamamını 1 santimetrelik kumla doldurmak için 7 trilyon 360 milyar 320 milyon kum tanesine ihtiyacımız var.",
        "**Biliyor muydunuz?**: Bir F1 aracı teorik olarak bir tünelde baş aşağı gidebilecek kadar downforce üretir.",
        "**Biliyor muydunuz?**: Pek çok kişi tarafından 'uğursuz' olarak kabul edilen 13 numaraya baktığımızda Pastor Maldonado tam 38 kez bu numarayla yarıştığını görüyoruz. Maldonado bu yarışların 16'sını tamamlayamazken sadece 7 kez puan alabildi.",
        "**Biliyor muydunuz?**: Bundan birkaç sene önce imkânsız olarak görülen fren diski teknolojileri artık F1 araçlarında yaygın olarak kullanılıyor. F1 araçları pistte birkaç tur attıktan sonra fren diskleri 1.000 santigrat dereceye ulaşabiliyor. Bu yanardağlardan çıkan erimiş lavların ısısına eş değer olarak belirtiliyor."
        ]
    
    response = random.choice(bm_quotes)
    await context.message.channel.send(response)

@bot.command(name = 'foto')
async def foto(context):
    fotolar = ['resim1.png', 'resim2.jpg', 'resim3.jpg', 'resim4.gif' ]
    random_foto = random.choice(fotolar)
    await context.send(file = discord.File(random_foto))

@bot.command(name = 'ders')
async def ders(ctx):
    study_rol = discord.utils.get(ctx.guild.roles, id = 789459764685701150)
    
    await ctx.channel.send(f"Hadi ders çalışın {study_rol.mention}")

@bot.command(name = 'pikaçu')
async def pikachu_foto(ctx):
    """Random pikacu gifi falan atar"""

    URL = 'https://some-random-api.ml/img/pikachu'
    
    async with request("GET", URL, headers= {}) as response:
        if response.status == 200:
            data = await response.json()
            await ctx.send(data)
        
        else:
            await ctx.send("Bir sey yanlis gitti")





bot.run("Njk2NjkwNTE1ODczMzY2MDY2.XosZmg.IWO6NBtE_6eWTRmgtI4_s14lPZ0")