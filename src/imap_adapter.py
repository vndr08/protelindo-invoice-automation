"""Adapter IMAP read-only berbasis UID untuk intake tahap berikutnya."""
from __future__ import annotations
import imaplib
from dataclasses import dataclass

class ImapAdapterError(RuntimeError): pass
@dataclass(frozen=True)
class ImapMessage: uidvalidity:str; uid:str; raw:bytes
@dataclass(frozen=True)
class ImapUid: uidvalidity:str; uid:str
class ReadOnlyImapAdapter:
    """Tidak mengubah SEEN/flag; fetch memakai BODY.PEEK[]."""
    def __init__(self,host:str,port:int,username:str,password:str,mailbox:str='INBOX',timeout:int=30): self.args=(host,port,username,password,mailbox,timeout)

    @staticmethod
    def _uidvalidity(client: imaplib.IMAP4_SSL) -> str:
        """Menangani format UIDVALIDITY standar dan variasi respons Gmail tanpa mengekspos mailbox."""
        _,response=client.response('UIDVALIDITY')
        for item in response or []:
            if isinstance(item,tuple) and len(item) >= 2:
                value=item[1]
            else:
                value=item
            if isinstance(value,bytes): return value.decode(errors='replace')
            if value is not None: return str(value)
        return '0'

    def _fetch_uids(self, uids:list[bytes]) -> list[ImapMessage]:
        """Fetch UID tertentu dengan sesi read-only agar re-fetch tidak mengubah flag email."""
        host,port,user,password,mailbox,timeout=self.args
        try:
            with imaplib.IMAP4_SSL(host,port,timeout=timeout) as client:
                client.login(user,password); status,_=client.select(mailbox,readonly=True)
                if status!='OK': raise ImapAdapterError('read-only mailbox select failed')
                validity=self._uidvalidity(client)
                result=[]
                for uid in uids:
                    # BODY.PEEK memastikan FETCH tidak menandai message sebagai seen.
                    status,payload=client.uid('fetch',uid,'(BODY.PEEK[])')
                    if status=='OK' and payload and isinstance(payload[0],tuple):
                        result.append(ImapMessage(validity,uid.decode(),payload[0][1]))
                return result
        except (imaplib.IMAP4.error,OSError) as error:
            raise ImapAdapterError('IMAP connection or authentication failed') from error

    def fetch_unseen(self)->list[ImapMessage]:
        host,port,user,password,mailbox,timeout=self.args
        try:
            with imaplib.IMAP4_SSL(host,port,timeout=timeout) as client:
                client.login(user,password); status,data=client.select(mailbox,readonly=True)
                if status!='OK': raise ImapAdapterError('read-only mailbox select failed')
                validity=self._uidvalidity(client)
                status,data=client.uid('search',None,'UNSEEN')
                if status!='OK': raise ImapAdapterError('UID search failed')
                result=[]
                for uid in data[0].split():
                    status,payload=client.uid('fetch',uid,'(BODY.PEEK[])')
                    if status=='OK' and payload and isinstance(payload[0],tuple): result.append(ImapMessage(validity,uid.decode(),payload[0][1]))
                return result
        except (imaplib.IMAP4.error,OSError) as error: raise ImapAdapterError('IMAP connection or authentication failed') from error

    def fetch_uids(self, uids:list[str]) -> list[ImapMessage]:
        """Mengambil ulang UID tertentu untuk state NEEDS_REFETCH tanpa UID SEARCH baru."""
        return self._fetch_uids([uid.encode() for uid in uids if uid])

    def list_unseen_uids(self, limit: int = 20) -> list[ImapUid]:
        """Menampilkan UID UNSEEN terbaru dalam jumlah terbatas tanpa FETCH body."""
        if not 1 <= limit <= 100: raise ValueError('UID listing limit must be between 1 and 100')
        host,port,user,password,mailbox,timeout=self.args
        try:
            with imaplib.IMAP4_SSL(host,port,timeout=timeout) as client:
                client.login(user,password); status,_=client.select(mailbox,readonly=True)
                if status!='OK': raise ImapAdapterError('read-only mailbox select failed')
                validity=self._uidvalidity(client)
                status,data=client.uid('search',None,'UNSEEN')
                if status!='OK': raise ImapAdapterError('UID search failed')
                # UID makin besar berarti message lebih baru; batasi output agar aman untuk mailbox besar.
                uids=(data[0] if data else b'').split()[-limit:]
                return [ImapUid(validity,uid.decode()) for uid in uids]
        except (imaplib.IMAP4.error,OSError) as error:
            raise ImapAdapterError('IMAP connection or authentication failed') from error
